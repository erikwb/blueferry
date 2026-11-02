"""The GTK TaskRunner: work on its own pool, answers on the GLib main loop."""
from __future__ import annotations

import threading
import time

import pytest
from gi.repository import GLib

from blueferry.ui.tasks import WorkerRunner


def _iterate_until(condition, timeout: float = 3.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        context.iteration(False) or time.sleep(0.005)


@pytest.mark.real_glib_sources
def test_answers_arrive_on_the_main_loop_in_contract_order() -> None:
    runner = WorkerRunner("test")
    events: list[tuple] = []
    main = threading.get_ident()
    runner.submit(lambda: threading.get_ident(),
                  lambda worker: events.append(("done", worker != main, threading.get_ident())),
                  lambda message: events.append(("error", message)),
                  lambda: events.append(("finished",)))

    def fail() -> None:
        raise ValueError("no plugin")

    runner.submit(fail, lambda _value: events.append(("done",)),
                  lambda message: events.append(("error", message)),
                  lambda: events.append(("finished",)))
    runner.submit(lambda: 1)  # quiet work without callbacks
    _iterate_until(lambda: len(events) == 4)
    assert events == [("done", True, main), ("finished",), ("error", "no plugin"), ("finished",)]
    runner.stop()


@pytest.mark.real_glib_sources
def test_a_stopped_runner_delivers_nothing_and_takes_no_work() -> None:
    runner = WorkerRunner("test")
    gate = threading.Event()
    events: list[str] = []
    runner.submit(gate.wait, lambda _value: events.append("late"))
    runner.stop()
    gate.set()
    runner.submit(lambda: 1, lambda _value: events.append("after stop"))
    deadline = time.monotonic() + 0.2
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)
    assert events == [] and runner.stopped
