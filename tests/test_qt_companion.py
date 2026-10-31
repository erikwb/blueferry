"""Qt adapter for the companion tools: worker hand-off and state, fake system."""
from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from blueferry import companion_tools
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


def test_blocking_actions_run_one_at_a_time_and_report(app, tmp_path, track) -> None:
    fake = Fake(tmp_path, installed=PHOTO_TOOLS, answers={
        **DEVICE, ("idevicepair", "validate"): CommandResult(1, "not paired"),
    })
    tools = track(CompanionTools(fake.system()))
    reports: list[tuple[bool, str]] = []
    tools.reported.connect(lambda ok, message: reports.append((ok, message)))
    tools.run("photos")
    assert tools.state()["busy"] == "photos"
    tools.run("eject")  # ignored while busy
    _wait(tools, lambda: tools.state()["busy"] == "" and reports)
    assert tools.state()["needsPairing"] is True
    assert reports[0][0] is False and "Trust This Computer" in reports[0][1]
    assert ["fusermount3", "-u"] not in [argv[:2] for argv in fake.ran]

    fake.answers[("idevicepair", "pair")] = CommandResult(0, "SUCCESS")
    tools.run("pair")
    _wait(tools, lambda: len(reports) == 2 and tools.state()["busy"] == "")
    assert tools.state()["needsPairing"] is False
    tools.clear_message()
    assert tools.state()["message"] == ""


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


@pytest.mark.parametrize("exits_before_click", [False, True])
def test_replaced_mirror_row_can_only_stop(app, tmp_path, track, exits_before_click):
    fake = Fake(tmp_path, installed={"uxplay"})
    system = fake.system()
    assert companion_tools.perform(system, "mirror").ok
    fake.processes[4242] = ["uxplay"]
    launcher = next(iter(fake.commands.values()))
    fake.replaced = frozenset({"uxplay"})
    tools = track(CompanionTools(system))
    tools.refresh()
    _wait(tools, lambda: tools.state()["probed"])
    mirror = tools.snapshot.get("mirror")
    assert mirror is not None and mirror.active and mirror.title == "Stop mirroring"
    if exits_before_click:
        fake.processes.clear()
    reports = []
    tools.reported.connect(lambda *args: reports.append(args))
    tools.run("mirror")
    _wait(tools, lambda: reports and not tools.busy)
    assert tools.state()["messageOk"] is not exits_before_click
    assert bool(fake.killed) is not exits_before_click
    assert next(iter(fake.commands.values())) is launcher  # no new launcher
    _wait(tools, lambda: tools.snapshot.get("mirror") is None)


@pytest.mark.parametrize("action, replacement", [("mirror", "uxplay"), ("send", "localsend")])
def test_action_rechecks_replacements_after_snapshot(app, tmp_path, track, action, replacement):
    fake = Fake(tmp_path, installed={"uxplay", "localsend"})
    tools = track(CompanionTools(fake.system()))
    tools.refresh()
    _wait(tools, lambda: tools.state()["probed"])
    assert tools.snapshot.get(action).enabled
    # Another client enables a replacement after the last UI refresh.
    fake.replaced = frozenset({replacement})
    reports = []
    tools.reported.connect(lambda *args: reports.append(args))
    tools.run(action)
    _wait(tools, lambda: reports and not tools.busy)
    assert tools.state()["messageOk"] is False
    assert not fake.commands
