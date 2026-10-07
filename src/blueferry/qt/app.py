"""PySide6/Kirigami application entry point."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from importlib.resources import files

from PySide6.QtCore import QLibraryInfo, QLocale, QTimer, QTranslator, QUrl
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from blueferry.client_activation import record_client_use
from blueferry.qt.activation import ClientActivation
from blueferry.qt.controller import BridgeController

APP_ID = "io.weirdware.BlueFerry.Qt"
APP_ICON = "io.weirdware.BlueFerry"
TRANSLATION_DIR = os.environ.get(
        "BLUEFERRY_QT_LOCALE_DIR", "/usr/share/blueferry/translations"
        )
# KDE's style from qqc2-desktop-style, else Qt's own desktop style.
QUICK_CONTROLS_STYLES = ("org.kde.desktop", "Fusion")
QUICK_CONTROLS_BINDING = "PySide6.QtQuickControls2"

log = logging.getLogger(__name__)


def _quick_style_binding():
    """Return QQuickStyle, or None where PySide6 lacks the binding.

    Only a missing binding is expected. A binding that is installed but
    fails to load is logged so the fallback does not hide the breakage.
    """
    try:
        from PySide6.QtQuickControls2 import QQuickStyle
    except ImportError as error:
        missing = (
            isinstance(error, ModuleNotFoundError)
            and error.name == QUICK_CONTROLS_BINDING
        )
        if not missing:
            log.warning(
                "%s could not be imported; using the environment fallback",
                QUICK_CONTROLS_BINDING, exc_info=True,
            )
        return None
    return QQuickStyle


def _qml_import_paths() -> list[str]:
    """Return the directories Qt searches for QML modules, in Qt's order."""
    paths: list[str] = []
    for variable in ("QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        paths.extend(
            path for path in os.environ.get(variable, "").split(os.pathsep)
            if path
        )
    paths.append(QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath))
    return paths


def _quick_controls_style_installed(name: str) -> bool:
    """Tell whether Qt Quick Controls can resolve the style module name.

    A dotted name is a module URI; a plain name is one of Qt's own styles
    under QtQuick.Controls. A style Qt cannot resolve makes every QML file
    that imports QtQuick.Controls fail with "module ... is not installed".
    """
    module = name if "." in name else f"QtQuick.Controls.{name}"
    relative = os.path.join(*module.split("."), "qmldir")
    return any(
        os.path.isfile(os.path.join(path, relative))
        for path in _qml_import_paths()
    )


def _split_style_argument(qt_args: list[str]) -> tuple[list[str], str | None]:
    """Remove the forms of -style that QApplication honours.

    Qt 6 accepts "-style NAME", "-style=NAME" and both again with two
    dashes; the last one given wins.
    """
    remaining: list[str] = []
    style = None
    index = 0
    while index < len(qt_args):
        argument = qt_args[index]
        if argument in ("-style", "--style") and index + 1 < len(qt_args):
            style = qt_args[index + 1]
            index += 2
            continue
        for prefix in ("-style=", "--style="):
            if argument.startswith(prefix):
                style = argument[len(prefix):]
                break
        else:
            remaining.append(argument)
        index += 1
    return remaining, style


class _QuickControlsStyle:
    """Default the Qt Quick Controls style to KDE's unless the user chose one.

    KDE's style comes from qqc2-desktop-style. Where it is not installed,
    Qt's Fusion style is chosen the same way instead: naming a style Qt
    cannot find would stop Main.qml from loading at all, and leaving the
    choice to Qt would let a widget style name from -style or
    QT_STYLE_OVERRIDE fail in the same way.

    This must be created before QApplication and before the QML engine
    loads Qt Quick Controls. Where the PySide6.QtQuickControls2 binding
    exists, QQuickStyle.setStyle() is used; it outranks every other way of
    choosing a style.

    Some distributions omit that binding; there QT_QUICK_CONTROLS_STYLE is
    set instead. Qt Quick Controls ranks that variable below the widget
    style QApplication takes from -style or QT_STYLE_OVERRIDE, and a widget
    style name such as "fusion" or "kvantum" is no Controls style, so
    Main.qml would fail to load. On this path the widget style is therefore
    kept away from QApplication's constructor and applied afterwards with
    QApplication.setStyle(), which changes the widget style only.
    release_environment() removes the fallback variable again, so
    processes started from the app (links, file managers) do not inherit it.
    """

    def __init__(self, qt_args: list[str]) -> None:
        self.qt_args = list(qt_args)
        self._fallback = False
        self._widget_style: str | None = None
        self._hidden_override: str | None = None
        user_style = os.environ.get("QT_QUICK_CONTROLS_STYLE")
        self.controls_style: str | None = user_style or None
        self.how = "QT_QUICK_CONTROLS_STYLE set by the user"
        if user_style:
            return
        style = next(
            (name for name in QUICK_CONTROLS_STYLES
             if _quick_controls_style_installed(name)),
            None,
        )
        if style is None:
            names = ", ".join(QUICK_CONTROLS_STYLES)
            self.how = f"left to Qt: none of {names} is installed"
            log.warning("Controls style %s", self.how)
            return
        if style != QUICK_CONTROLS_STYLES[0]:
            log.info("%s is not installed; using %s", QUICK_CONTROLS_STYLES[0], style)
        self.controls_style = style
        binding = _quick_style_binding()
        if binding is not None:
            binding.setStyle(style)
            self.how = "QQuickStyle.setStyle()"
            return
        os.environ["QT_QUICK_CONTROLS_STYLE"] = style
        self.how = f"QT_QUICK_CONTROLS_STYLE (no {QUICK_CONTROLS_BINDING} binding)"
        self._fallback = True
        self.qt_args, argument_style = _split_style_argument(self.qt_args)
        self._hidden_override = os.environ.pop("QT_STYLE_OVERRIDE", None)
        self._widget_style = argument_style or self._hidden_override or None

    def describe(self) -> str:
        """Explain the style decision for blueferry-qt --diagnose-style."""
        widget = (
            f"{self._widget_style} (applied after QApplication)"
            if self._widget_style
            else "left to Qt (-style, QT_STYLE_OVERRIDE, platform theme)"
        )
        lines = [
            f"Controls style: {self.controls_style or 'Qt default'}",
            f"Chosen through: {self.how}",
            f"Widget style:   {widget}",
            "QML import paths searched:",
            *(f"  {path}" for path in _qml_import_paths()),
        ]
        return "\n".join(lines)

    def application_created(self, application: QApplication) -> None:
        """Restore QT_STYLE_OVERRIDE and apply the user's widget style."""
        if self._hidden_override is not None:
            os.environ["QT_STYLE_OVERRIDE"] = self._hidden_override
            self._hidden_override = None
        if self._widget_style:
            application.setStyle(self._widget_style)

    def release_environment(self) -> None:
        """Forget the fallback variable once Qt has resolved the style."""
        if self._fallback:
            os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)
            self._fallback = False


def _select_quick_controls_style(
        qt_args: list[str] | None = None,
        ) -> _QuickControlsStyle:
    return _QuickControlsStyle(qt_args or [])


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
    parser.add_argument("--diagnose-style", action="store_true")
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    if args.diagnose_style:
        print(_select_quick_controls_style(qt_args).describe())
        return 0
    wayland_token = os.environ.pop("XDG_ACTIVATION_TOKEN", "")
    token = wayland_token or os.environ.get("DESKTOP_STARTUP_ID", "")
    quick_style = _select_quick_controls_style(qt_args)

    application = QApplication([sys.argv[0], *quick_style.qt_args])
    quick_style.application_created(application)
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
        quick_style.release_environment()
        return 0 if activation.forward(args.message, token) else 1

    controller = BridgeController(parent=application)
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"bridge": controller})
    qml = files("blueferry.qt").joinpath("qml/Main.qml")
    engine.load(QUrl.fromLocalFile(str(qml)))
    quick_style.release_environment()
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
