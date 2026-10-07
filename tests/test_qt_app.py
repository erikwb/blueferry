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


STYLE_VARIABLES = (
    "QT_QUICK_CONTROLS_STYLE", "QT_STYLE_OVERRIDE", "DESKTOP_STARTUP_ID",
)


@pytest.fixture(autouse=True)
def isolated_style_environment(monkeypatch):
    """Let monkeypatch restore variables the code under test writes directly.

    delenv() on an unset variable records nothing to undo, so a value the
    application later puts into os.environ would outlive the test. setenv()
    first records the original state, including "unset".
    """
    for name in STYLE_VARIABLES:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def quick_style_binding(monkeypatch):
    """Install a stub PySide6.QtQuickControls2 and return the styles set."""
    styles = []
    binding = types.ModuleType("PySide6.QtQuickControls2")
    binding.QQuickStyle = types.SimpleNamespace(setStyle=styles.append)
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", binding)
    return styles


@pytest.fixture
def no_quick_style_binding(monkeypatch):
    """Make importing PySide6.QtQuickControls2 fail as on Gentoo."""
    monkeypatch.setitem(sys.modules, "PySide6.QtQuickControls2", None)


def test_kde_quick_controls_style_is_set_without_the_binding(
        monkeypatch, no_quick_style_binding,
        ):
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"

    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "")
    app_module._select_quick_controls_style()
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "org.kde.desktop"


def test_kde_quick_controls_style_uses_the_binding_when_available(
        quick_style_binding,
        ):
    app_module._select_quick_controls_style()

    assert quick_style_binding == ["org.kde.desktop"]
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_user_quick_controls_style_is_preserved(monkeypatch, quick_style_binding):
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Fusion")

    app_module._select_quick_controls_style()

    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Fusion"
    assert quick_style_binding == []


def test_missing_binding_falls_back_quietly(caplog, no_quick_style_binding):
    with caplog.at_level("DEBUG", logger=app_module.__name__):
        assert app_module._quick_style_binding() is None
    assert caplog.records == []


@pytest.mark.parametrize("error", [
    ImportError("libQt6QuickControls2.so.6: undefined symbol"),
    ModuleNotFoundError("No module named 'shiboken6'", name="shiboken6"),
])
def test_broken_binding_is_logged_before_falling_back(monkeypatch, caplog, error):
    import builtins

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "PySide6.QtQuickControls2":
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "PySide6.QtQuickControls2", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    with caplog.at_level("WARNING", logger=app_module.__name__):
        assert app_module._quick_style_binding() is None
    assert [record.exc_info[1] for record in caplog.records] == [error]


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
