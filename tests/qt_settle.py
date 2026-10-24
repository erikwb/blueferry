"""Event-loop helpers for the Qt adapter tests (plugins, surfaces, tools).

``wait`` spins a local loop with owned timers: a ``QTimer.singleShot`` would
fire later on a loop that is gone and crash the suite. ``Settler`` remembers
the adapters a test created and, after the test, stops their timers, waits
for their private pools and delivers the queued signals, so no task reports
to an object that a later test's event loop no longer knows.

``main_thread_gc`` keeps Python's cyclic garbage collector off the worker
threads: a collection that runs while a pool thread executes Python code
would destroy unreachable QObjects of earlier tests (QML items, timers)
on that thread, and a timer destroyed there stays registered with the main
loop and crashes it later. The fixture collects on the main thread,
pauses automatic collection for the test and collects again afterwards.
"""
from __future__ import annotations

import gc
from collections.abc import Callable, Iterator
from typing import TypeVar

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

T = TypeVar("T")


def wait(predicate: Callable[[], object], timeout_ms: int = 5000) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.start(10)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout_ms)
    loop.exec()
    timer.stop()
    deadline.stop()
    assert predicate()


class Settler:
    def __init__(self) -> None:
        self._adapters: list[object] = []

    def __call__(self, adapter: T) -> T:
        self._adapters.append(adapter)
        return adapter

    def settle(self) -> None:
        for adapter in self._adapters:
            settle(adapter)
        self._adapters.clear()


def settle(adapter: object) -> None:
    """Stop an adapter's timers and let its pool and queued signals drain."""
    cancel = getattr(adapter, "cancel_sign_in", None)
    if cancel is not None:
        cancel()
    for name in ("_login_timer", "_timer"):
        timer = getattr(adapter, name, None)
        if timer is not None:
            timer.stop()
    for _round in range(3):  # a finished task may queue the next one
        adapter._pool.waitForDone(5000)  # type: ignore[attr-defined]
        QCoreApplication.processEvents()


@pytest.fixture
def main_thread_gc() -> Iterator[None]:
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()
        gc.collect()
