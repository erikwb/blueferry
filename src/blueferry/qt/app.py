"""PySide6/Kirigami application entry point."""
from __future__ import annotations

import argparse
import os
import signal
import sys
from importlib.resources import files

from PySide6.QtCore import QLocale, QTimer, QTranslator, QUrl
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from blueferry.client_activation import record_client_use
from blueferry.qt.activation import ClientActivation
from blueferry.qt.controller import BridgeController

APP_ID = "io.weirdware.BlueFerry.Qt"
APP_ICON = "io.weirdware.BlueFerry"
TRANSLATION_DIR = os.environ.get(
        "BLUEFERRY_QT_LOCALE_DIR", "/usr/share/blueferry/translations"
        )


def _install_translation(application: QGuiApplication) -> None:
    translator = QTranslator(application)
    if translator.load(
            QLocale.system(), "blueferry", "_", TRANSLATION_DIR,
            ):
        application.installTranslator(translator)


def _install_terminal_signal_handlers(application: QGuiApplication) -> QTimer:
    """Make SIGINT/SIGTERM observable while Qt owns the main thread."""
    timer = QTimer(application)
    timer.setInterval(250)
    timer.timeout.connect(lambda: None)
    timer.start()

    handled = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in handled}

    def quit_application(_signum, _frame) -> None:
        application.quit()

    for signum in handled:
        signal.signal(signum, quit_application)

    def restore_handlers() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    application.aboutToQuit.connect(restore_handlers)
    return timer


def _present_window(window: QWindow, token: str | None = None) -> None:
    # Qt Wayland consumes XDG_ACTIVATION_TOKEN in requestActivate(), including
    # for an already-created window. Set it before show() as that can activate.
    if token:
        os.environ["XDG_ACTIVATION_TOKEN"] = token
    window.show()
    window.raise_()
    window.requestActivate()
    os.environ.pop("XDG_ACTIVATION_TOKEN", None)
    record_client_use("qt")


def _create_system_tray(
        application: QApplication,
        window: QWindow,
        ) -> QSystemTrayIcon | None:
    """Expose the KDE/desktop status-notifier item for the Qt client."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None

    icon = QIcon.fromTheme("smartphone-symbolic")
    if icon.isNull():
        icon = QIcon.fromTheme("smartphone")
    if icon.isNull():
        icon = QIcon.fromTheme(APP_ICON)
    tray = QSystemTrayIcon(icon, application)
    tray.setToolTip("BlueFerry")

    menu = QMenu()
    show_action = QAction("Open BlueFerry", menu)
    show_action.triggered.connect(lambda: _present_window(window))
    menu.addAction(show_action)
    menu.addSeparator()
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(application.quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    # QSystemTrayIcon owns only a guarded pointer to its menu.
    tray._blueferry_menu = menu

    def activated(reason) -> None:
        if reason in (
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
                ):
            _present_window(window)

    tray.activated.connect(activated)
    tray.show()
    application.setQuitOnLastWindowClosed(False)
    return tray


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--message", default="")
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    wayland_token = os.environ.pop("XDG_ACTIVATION_TOKEN", "")
    token = wayland_token or os.environ.get("DESKTOP_STARTUP_ID", "")
    if not os.environ.get("QT_QUICK_CONTROLS_STYLE"):
        QQuickStyle.setStyle("org.kde.desktop")

    application = QApplication([sys.argv[0], *qt_args])
    # The X11 platform reads its startup ID while constructing QApplication.
    os.environ.pop("DESKTOP_STARTUP_ID", None)
    application.setApplicationName("blueferry")
    application.setApplicationDisplayName("BlueFerry")
    application.setOrganizationDomain("weirdware.io")
    application.setDesktopFileName(APP_ID)
    application.setWindowIcon(QIcon.fromTheme(APP_ICON))
    _install_translation(application)
    activation = ClientActivation(application)
    if not activation.primary:
        return 0 if activation.forward(args.message, token) else 1

    controller = BridgeController(parent=application)
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"bridge": controller})
    qml = files("blueferry.qt").joinpath("qml/Main.qml")
    engine.load(QUrl.fromLocalFile(str(qml)))
    if not engine.rootObjects():
        activation.close()
        return 1
    window = engine.rootObjects()[0]
    pending_token: str | None = None

    def present_message(_handle: str) -> None:
        nonlocal pending_token
        focus_token, pending_token = pending_token, None
        _present_window(window, focus_token)

    def activate_message(handle: str, activation_token: str) -> None:
        nonlocal pending_token
        pending_token = activation_token
        if handle:
            controller.messageOpenRequested.emit(handle)
        else:
            present_message("")

    activation.requested.connect(activate_message)
    controller.messageOpenRequested.connect(present_message)
    window.activeChanged.connect(lambda: record_client_use("qt") if window.isActive() else None)
    def ready() -> None:
        activate_message(args.message, token)
        activation.ready()

    QTimer.singleShot(0, ready)
    system_tray = _create_system_tray(application, window)
    terminal_signal_timer = _install_terminal_signal_handlers(application)
    exit_code = application.exec()
    terminal_signal_timer.stop()
    activation.close()
    if system_tray is not None:
        system_tray.hide()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
