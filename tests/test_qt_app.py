"""Qt process integration that does not construct a graphical application."""
from __future__ import annotations

import os
import signal
import sys
import types

import pytest

pytest.importorskip("PySide6")

from blueferry.qt import app as app_module


def test_focus_token_is_available_before_show_and_does_not_leak(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "record_client_use", lambda *_args: None)

    class Window:
        def show(self):
            calls.append(("show", os.environ.get("XDG_ACTIVATION_TOKEN")))

        def raise_(self):
            pass

        def requestActivate(self):
            calls.append(("activate", os.environ.get("XDG_ACTIVATION_TOKEN")))

    app_module._present_window(Window(), "focus-token")
    app_module._present_window(Window())
    assert calls == [
        ("show", "focus-token"), ("activate", "focus-token"),
        ("show", None), ("activate", None),
    ]


def test_kde_quick_controls_style_is_set_without_the_binding(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", None)
    monkeypatch.delenv("QT_QUICK_CONTROLS_STYLE", raising=False)
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"

    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "")
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"


def test_kde_quick_controls_style_uses_the_binding_when_available(monkeypatch):
    styles = []
    binding = types.ModuleType("PySide6.QtQuickControls2")
    binding.QQuickStyle = types.SimpleNamespace(setStyle=styles.append)
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", binding)
    monkeypatch.delenv("QT_QUICK_CONTROLS_STYLE", raising=False)

    app_module._select_quick_controls_style()

    assert styles == ["org.kde.desktop"]
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_user_quick_controls_style_is_preserved(monkeypatch):
    styles = []
    binding = types.ModuleType("PySide6.QtQuickControls2")
    binding.QQuickStyle = types.SimpleNamespace(setStyle=styles.append)
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", binding)
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Fusion")

    app_module._select_quick_controls_style()

    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Fusion"
    assert styles == []


def test_style_is_selected_before_qt_application_and_qml_engine_exist(monkeypatch):
    events = []

    class Application:
        def __init__(self, argv) -> None:
            events.append("application")

        def __getattr__(self, _name):
            return lambda *_args: None

    class Activation:
        primary = True

        def __init__(self, _application) -> None:
            pass

        def close(self) -> None:
            events.append("close")

    class Engine:
        def __init__(self) -> None:
            events.append("engine")

        def setInitialProperties(self, _properties) -> None:
            pass

        def load(self, _url) -> None:
            events.append("load")

        def rootObjects(self):
            return []

    monkeypatch.setattr(sys, "argv", ["blueferry-qt"])
    monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
    monkeypatch.setattr(
        app_module, "_select_quick_controls_style", lambda: events.append("style"),
    )
    monkeypatch.setattr(app_module, "QApplication", Application)
    monkeypatch.setattr(
        app_module, "QIcon", types.SimpleNamespace(fromTheme=lambda _name: None),
    )
    monkeypatch.setattr(app_module, "_install_translation", lambda _application: None)
    monkeypatch.setattr(app_module, "ClientActivation", Activation)
    monkeypatch.setattr(app_module, "BridgeController", lambda *, parent: object())
    monkeypatch.setattr(app_module, "QQmlApplicationEngine", Engine)

    assert app_module.main() == 1
    assert events == ["style", "application", "engine", "load", "close"]


class _Signal:
    def __init__(self) -> None:
        self.callback = None

    def connect(self, callback) -> None:
        self.callback = callback

    def emit(self) -> None:
        if self.callback is not None:
            self.callback()


def test_terminal_signals_quit_qt_and_restore_previous_handlers(monkeypatch):
    class Application:
        def __init__(self) -> None:
            self.aboutToQuit = _Signal()
            self.quit_calls = 0

        def quit(self) -> None:
            self.quit_calls += 1

    class Timer:
        def __init__(self, parent) -> None:
            self.parent = parent
            self.timeout = _Signal()
            self.interval = 0
            self.started = False

        def setInterval(self, value: int) -> None:
            self.interval = value

        def start(self) -> None:
            self.started = True

    installed = {}
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    monkeypatch.setattr(app_module, "QTimer", Timer)
    monkeypatch.setattr(
        app_module.signal,
        "getsignal",
        lambda signum: previous[signum],
    )
    monkeypatch.setattr(
        app_module.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    application = Application()

    timer = app_module._install_terminal_signal_handlers(application)

    assert timer.interval == 250
    assert timer.started is True
    installed[signal.SIGINT](signal.SIGINT, None)
    assert application.quit_calls == 1

    application.aboutToQuit.emit()
    assert installed == previous
