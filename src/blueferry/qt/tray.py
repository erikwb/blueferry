"""``blueferry-tray``: a small system tray item for the running BlueFerry daemon.

Under Plasma ``QSystemTrayIcon`` becomes a StatusNotifierItem. The icon is a
phone with the number of unread messages painted over it, the tooltip shows
the connection plus battery, signal and network, and the menu opens the
BlueFerry client in use, switches the tray's own autostart entry and quits
the tray.

The tray is a thin client of the existing session-bus API. Every call is
asynchronous and sent with auto-start disabled: it follows a running daemon
and never starts (or restarts) one. GetStatus and ListThreads are fetched
again after the content-free StatusChanged and HistoryChanged signals,
coalesced by a short timer.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    SLOT,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRect,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtDBus import (
    QDBusConnection,
    QDBusMessage,
    QDBusPendingCallWatcher,
    QDBusServiceWatcher,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from blueferry import tray_autostart
from blueferry.client_activation import (
    ACTIVATION_INTERFACE,
    ACTIVATION_PATH,
    TRAY_BUS_NAME,
    DesktopClient,
    client_environment,
    client_program,
    is_live,
    ranked_clients,
)
from blueferry.client_wire import decode_mapping, decode_threads
from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.protocol import (
    BUS_NAME,
    EVENTS_IFACE,
    MESSAGES_IFACE,
    OBJECT_PATH,
    backend_compatibility_error,
)
from blueferry.qt import tray_presenter as presenter

APP_ICON = "io.weirdware.BlueFerry"
PHONE_ICONS = ("smartphone", "phone", APP_ICON)
ICON_SIZES = (16, 22, 24, 32, 48, 64)
THREAD_LIMIT = 500
CALL_TIMEOUT_MS = 10_000
REFRESH_DELAY_MS = 150
# Only used when the signal subscriptions failed: poll instead of going stale.
FALLBACK_POLL_MS = 30_000

log = logging.getLogger(__name__)

Reply = Callable[[list], None]
Failure = Callable[[str], None]


def phone_icon() -> QIcon:
    for name in PHONE_ICONS:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    # No icon theme at all: still show something the user can click.
    return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)


def badged_icon(base: QIcon, badge: str, *, dimmed: bool = False) -> QIcon:
    """The phone icon with ``badge`` in a red circle at its top right."""
    icon = QIcon()
    mode = QIcon.Mode.Disabled if dimmed else QIcon.Mode.Normal
    for size in ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base.paint(painter, QRect(0, 0, size, size), mode=mode)
        if badge:
            diameter = max(8, round(size * 0.5))
            width = min(size, max(diameter, round(diameter * (0.45 + 0.3 * len(badge)))))
            rect = QRect(size - width, 0, width, diameter)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#da4453"))  # Breeze "negative"
            painter.drawRoundedRect(rect, diameter / 2, diameter / 2)
            font = QFont()
            font.setBold(True)
            pixels = max(6, round(diameter * 0.75))
            font.setPixelSize(pixels)
            # Shrink long counts ("99+") until they fit inside the pill.
            while pixels > 6 and QFontMetrics(font).horizontalAdvance(badge) > width - 2:
                pixels -= 1
                font.setPixelSize(pixels)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class TrayController(QObject):
    """Owns the tray icon and mirrors the daemon's state into it."""

    def __init__(
        self,
        bus: QDBusConnection | None = None,
        *,
        parent: QObject | None = None,
        launch: Callable[[DesktopClient, str], bool] | None = None,
        autostart_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._bus = bus if bus is not None else QDBusConnection.sessionBus()
        self._launch = launch or launch_client
        self._autostart_path = autostart_path or tray_autostart.entry_path()
        self._watchers: set[QDBusPendingCallWatcher] = set()
        self._generation = 0
        self._pending: dict[str, object] = {}
        self._again: set[str] = set()
        self._closed = False
        self._daemon_owned: bool | None = None
        # A new daemon owner has its own history: reload the unread count.
        self._threads_stale = False
        self.status: BackendStatus | None = None
        self.error = ""
        self.unread = 0
        self._base_icon = phone_icon()

        self.tray = QSystemTrayIcon(self)
        self.menu = QMenu()
        self.open_action = QAction(_("Open BlueFerry"), self.menu)
        self.open_action.triggered.connect(self.open_app)
        self.autostart_action = QAction(_("Start at login"), self.menu)
        self.autostart_action.setCheckable(True)
        self.autostart_action.triggered.connect(self._autostart_triggered)
        self.quit_action = QAction(_("Quit Tray Icon"), self.menu)
        self.quit_action.triggered.connect(QApplication.quit)
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()
        self.menu.addAction(self.autostart_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        # The desktop's own autostart settings may change the entry too.
        self.menu.aboutToShow.connect(self.render_autostart)
        self.render_autostart()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        self._threads_timer = QTimer(self)
        self._threads_timer.setSingleShot(True)
        self._threads_timer.setInterval(REFRESH_DELAY_MS)
        self._threads_timer.timeout.connect(self.refresh_threads)

        self._service_watcher = QDBusServiceWatcher(
            BUS_NAME, self._bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self,
        )
        self._service_watcher.serviceOwnerChanged.connect(self._owner_changed)
        self.signals_connected = all(
            self._bus.connect(BUS_NAME, OBJECT_PATH, EVENTS_IFACE, name, self, SLOT(slot))
            for name, slot in (
                ("StatusChanged", "_status_invalidated()"),
                ("HistoryChanged", "_history_invalidated(QVariantMap)"),
            )
        )
        self._fallback_timer = QTimer(self)
        self._fallback_timer.setInterval(FALLBACK_POLL_MS)
        self._fallback_timer.timeout.connect(self._poll)
        if not self.signals_connected:
            # Without the signals the icon would never change again.
            log.warning("could not subscribe to BlueFerry signals; polling instead")
            self._fallback_timer.start()
        self.render()

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        # Shown once refresh() knows BlueFerry is set up (running or
        # activatable); an autostarted tray without a setup stays hidden.
        self.refresh()

    def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._pending.clear()
        self._again.clear()
        for timer in (self._refresh_timer, self._threads_timer, self._fallback_timer):
            timer.stop()
        self.tray.hide()
        self.menu.deleteLater()

    # ---- D-Bus ----------------------------------------------------------

    def _daemon_running(self) -> bool:
        # One synchronous lookup at start; afterwards the service watcher
        # keeps the answer current, so refreshes never block on the bus.
        if self._daemon_owned is None:
            self._daemon_owned = name_registered(self._bus, BUS_NAME)
        return self._daemon_owned

    def _call(
        self,
        service: str,
        interface: str,
        method: str,
        args: list,
        on_reply: Reply,
        on_error: Failure,
        *,
        timeout: int = CALL_TIMEOUT_MS,
        path: str = OBJECT_PATH,
    ) -> None:
        message = QDBusMessage.createMethodCall(service, path, interface, method)
        message.setArguments(args)
        # Never let a tray refresh activate a stopped daemon or client.
        message.setAutoStartService(False)
        watcher = QDBusPendingCallWatcher(self._bus.asyncCall(message, timeout), self)
        self._watchers.add(watcher)

        def finished(done: QDBusPendingCallWatcher) -> None:
            self._watchers.discard(done)
            done.deleteLater()
            if done.isError():
                on_error(done.error().name() or "")
            else:
                on_reply(done.reply().arguments())

        watcher.finished.connect(finished)

    def _load(
        self, method: str, args: list, on_reply: Reply, on_error: Failure,
        timer: QTimer, *, service: str = BUS_NAME, interface: str = MESSAGES_IFACE,
        path: str = OBJECT_PATH,
    ) -> None:
        """One refresh per method; discard replies from an earlier daemon."""
        if self._closed:
            return
        if method in self._pending:
            self._again.add(method)
            return
        token = self._pending[method] = object()
        generation = self._generation

        def completed(callback, value) -> None:
            if generation != self._generation or self._pending.get(method) is not token:
                return
            del self._pending[method]
            again = method in self._again
            self._again.discard(method)
            callback(value)
            if again and not self._closed:
                timer.start()

        self._call(
            service, interface, method, args,
            lambda reply: completed(on_reply, reply),
            lambda error: completed(on_error, error), path=path,
        )

    @Slot()
    def _status_invalidated(self) -> None:
        if not self._closed and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    @Slot("QVariantMap")
    def _history_invalidated(self, _revision) -> None:
        if not self._closed and not self._threads_timer.isActive():
            self._threads_timer.start()

    def _owner_changed(self, _name: str, _old: str, new: str) -> None:
        if self._closed:
            return
        self._generation += 1
        self._pending.clear()
        self._again.clear()
        self._daemon_owned = bool(new)
        self.apply_offline()
        # Both owner changes of a quick restart may fall into one coalesced
        # refresh, which then still sees the old status.
        self._threads_stale = True
        self._refresh_timer.start()

    @Slot()
    def _poll(self) -> None:
        self._threads_stale = True
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        if self._closed:
            return
        if not self._daemon_running():
            self.apply_offline()
            self._check_installed()
            return
        self.set_present(True)
        self._load(
            "GetStatus", [],
            lambda args: self.apply_status(args[0] if args else ""),
            lambda _name: self.apply_offline(), self._refresh_timer,
        )

    def _check_installed(self) -> None:
        """Hide the icon unless the bus could start the BlueFerry service."""
        if not self._bus.isConnected():
            self.set_present(True)  # cannot tell; keep the way back in
            return

        def listed(args: list) -> None:
            names = args[0] if args else []
            self.set_present(BUS_NAME in [str(name) for name in names or []])

        self._load(
            "ListActivatableNames", [],
            # Unknown: keep the icon rather than lose the only way back in.
            listed, lambda _name: self.set_present(True), self._refresh_timer,
            service="org.freedesktop.DBus", interface="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
        )

    def set_present(self, present: bool) -> None:
        """Show the icon for a usable BlueFerry setup, hide it otherwise."""
        self.tray.setVisible(present)

    @Slot()
    def refresh_threads(self) -> None:
        if self.status is None or not self._daemon_running():
            return
        self._load(
            # The signature says "u", but PySide6 can only marshal a Python
            # int as int32: QDBusArgument has no unsigned overload and
            # QVariant is not exposed. dbus-python does not enforce incoming
            # signatures, and the daemon bounds the limit anyway.
            "ListThreads", [THREAD_LIMIT],
            lambda args: self.apply_threads(args[0] if args else "[]"),
            lambda _name: None, self._threads_timer,
        )

    # ---- state ----------------------------------------------------------

    def apply_offline(self) -> None:
        self._pending.pop("ListThreads", None)
        self._again.discard("ListThreads")
        self.status = None
        self.error = ""
        self.unread = 0
        self.render()

    def apply_status(self, payload: object) -> None:
        try:
            raw = decode_mapping(payload)
        except ValueError:
            self.apply_offline()
            return
        incompatible = backend_compatibility_error(raw)
        if incompatible:
            self.apply_offline()
            self.error = _("The BlueFerry service needs an update.")
            self.render()
            return
        reload_threads = self.status is None or self._threads_stale
        self._threads_stale = False
        self.status = BackendStatus.from_dict(raw)
        self.error = ""
        self.render()
        if reload_threads:
            self.refresh_threads()

    def apply_threads(self, payload: object) -> None:
        try:
            threads = decode_threads(payload)
        except ValueError:
            return
        self.unread = presenter.unread_total(threads)
        self.render()

    def render(self) -> None:
        connected = self.status is not None and self.status.map
        self.tray.setIcon(badged_icon(
            self._base_icon, presenter.badge_text(self.unread), dimmed=not connected,
        ))
        self.tray.setToolTip(presenter.tooltip(self.status, self.unread, self.error))

    @Slot()
    def render_autostart(self) -> None:
        self.autostart_action.setChecked(tray_autostart.is_enabled(self._autostart_path))

    # ---- actions --------------------------------------------------------

    def _autostart_triggered(self, checked: bool) -> None:
        try:
            tray_autostart.set_enabled(bool(checked), self._autostart_path)
        except OSError:
            self.tray.showMessage(
                "BlueFerry", _("Could not change the autostart entry."),
                QSystemTrayIcon.MessageIcon.Warning, 5000,
            )
        self.render_autostart()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Qt maps the StatusNotifierItem calls to reasons: Activate is
        # Trigger, SecondaryActivate MiddleClick and ContextMenu Context.
        # Only a primary click opens the client; the host shows the menu.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_app()

    @Slot()
    def open_app(self) -> None:
        """Raise the client the user works with, or start the best one.

        The same choice as notifications and phone links: a running client
        first, then the most recently used one, then the desktop's own.
        """
        if self._closed:
            return
        # On Wayland a window may only take focus with the activation token
        # the panel handed to the tray for this click. It is single-use.
        token = take_activation_token()
        if not self._bus.isConnected():
            # A call on a dead connection never reports back.
            self._open_client([], token)
            return
        self._call(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "ListNames", [],
            lambda args: self._open_client(
                [str(name) for name in (args[0] if args else []) or []], token,
            ),
            lambda _name: self._open_client([], token),
            path="/org/freedesktop/DBus",
        )

    def _open_client(self, running: list[str], token: str) -> None:
        if self._closed:
            return
        candidates = ranked_clients(running)
        if not candidates:
            log.warning("no BlueFerry graphical client is installed")
            return
        client = candidates[0]
        if not is_live(client, running):
            self._launch_first(candidates, token)
            return
        if client.bus_name in running:
            service, path = client.bus_name, ACTIVATION_PATH
            interface, method = ACTIVATION_INTERFACE, "OpenMessage"
            args: list = ["", token]
        else:
            # A GTK window from before the shared activation protocol.
            service, path = client.desktop_id, "/" + client.desktop_id.replace(".", "/")
            interface, method = "org.freedesktop.Application", "Activate"
            platform_data = (
                {"activation-token": token, "desktop-startup-id": token} if token else {}
            )
            args = [platform_data]

        def failed(_error: str) -> None:
            if self._closed:
                return
            # The window may have closed since ListNames. Only start the
            # client again when its name is gone: a slow reply must not
            # open a second, competing window.
            self._call(
                "org.freedesktop.DBus", "org.freedesktop.DBus", "NameHasOwner", [service],
                lambda reply: None if reply and reply[0] else self._launch_first(
                    candidates, token,
                ),
                lambda _name: None, path="/org/freedesktop/DBus",
            )

        self._call(service, interface, method, args, lambda _args: None, failed, path=path)

    def _launch_first(self, candidates: list[DesktopClient], token: str) -> None:
        """Start the first client that launches; skip ones that cannot."""
        if self._closed:
            return
        for client in candidates:
            if self._launch(client, token):
                return
        log.warning("could not start a BlueFerry graphical client")


def take_activation_token() -> str:
    """The token for the current click, removed so no later click reuses it.

    Qt stores the token a panel provides through ProvideXdgActivationToken
    in this process's environment.
    """
    environment = QProcessEnvironment.systemEnvironment()
    token = (
        environment.value("XDG_ACTIVATION_TOKEN") or environment.value("DESKTOP_STARTUP_ID")
    )
    for name in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID"):
        os.environ.pop(name, None)
        os.unsetenv(name)
    return token if len(token) <= 4096 else ""


def launch_client(client: DesktopClient, token: str) -> bool:
    command = client_program(client)
    if command is None:
        return False
    process = QProcess()
    process.setProgram(command[0])
    process.setArguments(command[1:])
    environment = QProcessEnvironment()
    for name, value in client_environment(command, token).items():
        environment.insert(name, value)
    process.setProcessEnvironment(environment)
    # PySide6 returns a bare bool here; some bindings return (ok, pid).
    started = process.startDetached()
    return bool(started[0] if isinstance(started, tuple) else started)


def name_registered(bus: QDBusConnection, name: str) -> bool:
    """Whether ``name`` has an owner; this never activates the service."""
    interface = bus.interface() if bus.isConnected() else None
    if interface is None:
        return False
    reply = interface.isServiceRegistered(name)
    return bool(reply.isValid() and reply.value())


def _parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="blueferry-tray",
        description=_("BlueFerry's iPhone status and unread messages in the system tray."),
    )
    parser.add_argument(
        "--autostart", choices=("on", "off", "status"),
        help=_("start the tray at login (on), stop doing so (off), or show the setting"),
    )
    return parser.parse_known_args(argv)


def _autostart_command(choice: str) -> int:
    path = tray_autostart.entry_path()
    if choice != "status":
        try:
            tray_autostart.set_enabled(choice == "on", path)
        except OSError as error:
            print(f"blueferry-tray: {error.strerror or error}", file=sys.stderr)
            return 1
    print("on" if tray_autostart.is_enabled(path) else "off")
    return 0


def main() -> int:
    args, qt_args = _parse_arguments(sys.argv[1:])
    if args.autostart:
        return _autostart_command(args.autostart)
    application = QApplication([sys.argv[0], *qt_args])
    application.setApplicationName("blueferry-tray")
    application.setApplicationDisplayName("BlueFerry")
    application.setDesktopFileName("io.weirdware.BlueFerry.Tray")
    application.setQuitOnLastWindowClosed(False)
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        print("blueferry-tray: no session bus", file=sys.stderr)
        return 1
    if not bus.registerService(TRAY_BUS_NAME):
        return 0  # another tray is already running
    controller = TrayController(bus, parent=application)
    controller.start()
    signal.signal(signal.SIGINT, lambda *_args: application.quit())
    signal.signal(signal.SIGTERM, lambda *_args: application.quit())
    # Let Python see signals while Qt's loop runs.
    ticker = QTimer(application)
    ticker.start(500)
    ticker.timeout.connect(lambda: None)
    exit_code = application.exec()
    controller.close()
    bus.unregisterService(TRAY_BUS_NAME)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
