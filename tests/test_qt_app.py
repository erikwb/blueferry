"""Qt process integration that does not construct a graphical application."""
from __future__ import annotations

import signal

import pytest

pytest.importorskip("PySide6")

from blueferry.qt import app as app_module


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
