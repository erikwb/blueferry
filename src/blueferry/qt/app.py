"""PySide6/Kirigami application entry point."""
from __future__ import annotations

import os
import signal
import sys
from importlib.resources import files

from PySide6.QtCore import QLocale, QTimer, QTranslator, QUrl
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QWindow
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from blueferry.qt.controller import BridgeController

APP_ID = "io.weirdware.BlueFerry.Qt"
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


def _present_window(window: QWindow) -> None:
    window.show()
    window.raise_()
    window.requestActivate()


def _create_system_tray(
    application: QApplication,
    window: QWindow,
) -> QSystemTrayIcon | None:
    """Expose the KDE/desktop status-notifier item for the Qt client."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None

    icon = QIcon.fromTheme(APP_ID)
    if icon.isNull():
        icon = QIcon.fromTheme("phone-symbolic")
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
    if not os.environ.get("QT_QUICK_CONTROLS_STYLE"):
        QQuickStyle.setStyle("org.kde.desktop")

    application = QApplication(sys.argv)
    application.setApplicationName("blueferry")
    application.setApplicationDisplayName("BlueFerry")
    application.setOrganizationDomain("weirdware.io")
    application.setDesktopFileName(APP_ID)
    application.setWindowIcon(QIcon.fromTheme(APP_ID))
    _install_translation(application)

    controller = BridgeController(parent=application)
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({"bridge": controller})
    qml = files("blueferry.qt").joinpath("qml/Main.qml")
    engine.load(QUrl.fromLocalFile(str(qml)))
    if not engine.rootObjects():
        return 1
    window = engine.rootObjects()[0]
    controller.messageOpenRequested.connect(lambda _handle: _present_window(window))
    system_tray = _create_system_tray(application, window)
    terminal_signal_timer = _install_terminal_signal_handlers(application)
    exit_code = application.exec()
    terminal_signal_timer.stop()
    if system_tray is not None:
        system_tray.hide()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
