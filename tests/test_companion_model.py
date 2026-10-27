"""Companion tools state shared by the clients: no toolkit, fake system."""
from __future__ import annotations

from blueferry.companion_model import CompanionToolsModel
from blueferry.companion_tools import CommandResult
from tests.client_fakes import ManualRunner
from tests.test_companion_tools import DEVICE, PHOTO_TOOLS, Fake


def _model(work: ManualRunner, fake: Fake):
    changes: list[None] = []
    reports: list[tuple[bool, str]] = []
    model = CompanionToolsModel(
        runner=work, system=fake.system(), on_change=lambda: changes.append(None),
        on_report=lambda ok, message: reports.append((ok, message)),
    )
    return model, changes, reports


def test_refresh_probes_on_the_runner_and_exposes_rows(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay", *PHOTO_TOOLS}, answers=DEVICE)
    work = ManualRunner()
    tools, changes, _reports = _model(work, fake)
    assert tools.state()["tools"] == [] and fake.ran == []  # nothing on construction
    tools.refresh()
    tools.refresh()  # one probe at a time
    assert len(work.pending) == 1 and fake.ran == []
    work.run_tasks()
    assert tools.state()["probed"] and changes
    rows = {row["key"]: row for row in tools.state()["tools"]}
    assert rows["mirror"]["enabled"] and rows["photos"]["enabled"]
    assert not rows["send"]["installed"] and not rows["eject"]["enabled"]


def test_a_failed_probe_allows_the_next_one_and_changes_nothing(tmp_path) -> None:
    fake = Fake(tmp_path)
    work = ManualRunner()
    tools, changes, _reports = _model(work, fake)
    work.submit = lambda operation, done, failed, finished=None: failed("boom")  # type: ignore[method-assign]
    tools.refresh()
    assert changes == [] and tools.state()["probed"] is False
    assert tools._refreshing is False


def test_blocking_actions_run_one_at_a_time_and_report(tmp_path) -> None:
    fake = Fake(tmp_path, installed=PHOTO_TOOLS, answers={
        **DEVICE, ("idevicepair", "validate"): CommandResult(1, "not paired"),
    })
    work = ManualRunner()
    tools, _changes, reports = _model(work, fake)
    tools.run("photos")
    assert tools.state()["busy"] == "photos" and tools.busy == "photos"
    tools.run("eject")  # ignored while busy
    tools.run("nope")  # unknown actions never run
    assert len(work.pending) == 1
    work.run_tasks()
    assert tools.state()["busy"] == ""
    assert tools.state()["needsPairing"] is True and tools.needs_pairing
    assert reports[0][0] is False and "Trust This Computer" in reports[0][1]
    assert ["fusermount3", "-u"] not in [argv[:2] for argv in fake.ran]

    fake.answers[("idevicepair", "pair")] = CommandResult(0, "SUCCESS")
    tools.run("pair")
    work.run_tasks()
    assert len(reports) == 2 and tools.state()["needsPairing"] is False
    tools.clear_message()
    assert tools.state()["message"] == ""


def test_an_action_that_raises_reports_a_failure_and_refreshes(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    work = ManualRunner()
    tools, _changes, reports = _model(work, fake)
    tools.run("mirror")
    _operation, _done, failed, _finished = work.pending.pop(0)
    failed("crash")
    assert tools.state()["busy"] == ""
    assert reports == [(False, "The action failed unexpectedly.")]
    assert work.pending  # the refresh that follows every action


def test_mirroring_starts_on_the_runner(tmp_path) -> None:
    fake = Fake(tmp_path, installed={"uxplay"})
    work = ManualRunner()
    tools, _changes, _reports = _model(work, fake)
    tools.run("mirror")
    assert tools.state()["busy"] == "mirror" and not fake.commands  # handed to the worker
    work.run_tasks()
    assert list(fake.commands) == [("uxplay", "-n", "desk", "-nh")]
    assert tools.state()["messageOk"] is True and tools.state()["probed"]
