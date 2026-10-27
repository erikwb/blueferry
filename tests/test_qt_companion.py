"""Qt adapter for the companion tools: worker hand-off and signals, fake system.

The state logic is tested without Qt in ``tests/test_companion_model.py``.
"""
from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from blueferry.companion_tools import CommandResult
from blueferry.qt.companion import CompanionTools
from tests.qt_settle import Settler, main_thread_gc, wait
from tests.test_companion_tools import DEVICE, PHOTO_TOOLS, Fake

# Keep the cyclic GC off the pool threads (see tests/qt_settle.py).
pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)


@pytest.fixture(scope="module")
def app():
    # A GUI application: later Qt tests cannot replace a plain QCoreApplication.
    return QCoreApplication.instance() or QGuiApplication([])


def _wait(_tools: CompanionTools, predicate) -> None:
    wait(predicate)


@pytest.fixture
def track():
    settler = Settler()
    yield settler
    settler.settle()


def test_refresh_probes_on_the_worker_and_exposes_rows(app, tmp_path, track) -> None:
    fake = Fake(tmp_path, installed={"uxplay", *PHOTO_TOOLS}, answers=DEVICE)
    tools = track(CompanionTools(fake.system()))
    assert tools.state()["tools"] == [] and fake.ran == []  # nothing on construction
    tools.refresh()
    _wait(tools, lambda: tools.state()["probed"])
    rows = {row["key"]: row for row in tools.state()["tools"]}
    assert rows["mirror"]["enabled"] and rows["photos"]["enabled"]
    assert not rows["send"]["installed"] and not rows["eject"]["enabled"]


def test_actions_report_through_the_qt_signal(app, tmp_path, track) -> None:
    fake = Fake(tmp_path, installed=PHOTO_TOOLS, answers={
        **DEVICE, ("idevicepair", "validate"): CommandResult(1, "not paired"),
    })
    tools = track(CompanionTools(fake.system()))
    reports: list[tuple[bool, str]] = []
    changes: list[None] = []
    tools.reported.connect(lambda ok, message: reports.append((ok, message)))
    tools.changed.connect(lambda: changes.append(None))
    tools.run("photos")
    assert tools.busy == "photos" and changes
    _wait(tools, lambda: tools.state()["busy"] == "" and reports)
    assert tools.needs_pairing is True
    assert reports[0][0] is False and "Trust This Computer" in reports[0][1]


def test_mirroring_starts_on_the_worker_not_the_ui_thread(app, tmp_path, track) -> None:
    import threading

    fake = Fake(tmp_path, installed={"uxplay"})
    system = fake.system()
    threads: list[str] = []
    command_app = system.command_app

    def recording_command_app(argv):
        threads.append(threading.current_thread().name)
        return command_app(argv)

    system = dataclasses.replace(system, command_app=recording_command_app)
    tools = track(CompanionTools(system))
    tools.run("mirror")
    assert tools.state()["busy"] == "mirror"  # handed to the worker
    _wait(tools, lambda: tools.state()["busy"] == "" and fake.commands)
    assert list(fake.commands) == [("uxplay", "-n", "desk", "-nh")]
    assert threads and threading.main_thread().name not in threads
    assert tools.state()["messageOk"] is True
    _wait(tools, lambda: tools.state()["probed"])
