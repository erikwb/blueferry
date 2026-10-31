"""The shared models must receive every result on the Qt UI thread."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtGui import QGuiApplication

from blueferry.qt.controller import _WorkerRunner
from blueferry.qt.tasks import QtTaskRunner
from tests.qt_settle import main_thread_gc, wait

pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)


@pytest.mark.parametrize("fails", [False, True])
def test_task_results_and_finished_run_on_ui_thread_in_order(fails):
    app = QCoreApplication.instance() or QGuiApplication([])
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    runner = QtTaskRunner(pool)
    ui_thread = threading.get_ident()
    operations = []
    callbacks = []

    def work():
        operations.append(threading.get_ident())
        if fails:
            raise RuntimeError("expected error")
        return "result"

    def record(name, value=None):
        callbacks.append((name, value, threading.get_ident()))

    try:
        runner.submit(work, lambda value: record("done", value),
                      lambda error: record("error", error), lambda: record("finished"))
        wait(lambda: len(callbacks) == 2 and not runner._tasks)
        assert operations and operations[0] != ui_thread
        assert callbacks == [
            ("error" if fails else "done", "expected error" if fails else "result", ui_thread),
            ("finished", None, ui_thread),
        ]
    finally:
        assert pool.waitForDone(5000)
        app.processEvents()


@pytest.mark.parametrize("fails", [False, True])
def test_controller_runner_finishes_even_if_result_callback_raises(fails):
    finished = []

    def run(_work, done, error, **_kwargs):
        (error if fails else done)("result")

    def broken_callback(_value):
        raise RuntimeError("callback failed")

    runner = _WorkerRunner(SimpleNamespace(_run=run))
    with pytest.raises(RuntimeError, match="callback failed"):
        runner.submit(lambda: None, broken_callback, broken_callback,
                      lambda: finished.append(True))
    assert finished == [True]
