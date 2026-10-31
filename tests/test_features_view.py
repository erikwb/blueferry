"""Feature switches and the doctor report on the settings page: no toolkit."""
from __future__ import annotations

from blueferry import doctor_report, features_view
from blueferry.doctor_report import DoctorReport, parse_output


def test_features_load_fall_back_and_explain_a_change() -> None:
    state = features_view.initial()
    assert state == {"loaded": False, "available": False, "items": {}, "notice": "",
                     "error": ""}
    items = {"mark_read_on_dismiss": {"value": False, "restart_required": True}}
    state = features_view.with_notice(features_view.loaded(state, items), "kept")
    assert state["available"] is True and state["items"] == items
    assert state["notice"] == "kept"  # a reload keeps the last notice
    assert features_view.loaded(state, None)["items"] == {}
    # Older daemons have no GetFeatures: the UI falls back to hints.
    old = features_view.unavailable(state, "No such method 'GetFeatures'")
    assert old["loaded"] is True and old["available"] is False and old["items"] == {}
    assert old["error"] == "No such method 'GetFeatures'"
    assert "Restart" in features_view.set_notice("restart-required")
    assert "environment" in features_view.set_notice("environment")
    assert features_view.set_notice("applied") == "" and features_view.set_notice(None) == ""


def test_doctor_state_follows_the_run() -> None:
    state = doctor_report.idle_state()
    assert state["ran"] is False and state["running"] is False
    state = doctor_report.running_state(state)
    assert state["running"] is True
    done = doctor_report.finished_state(state, DoctorReport(True, True, "WARNING something"))
    assert done == {"running": False, "ran": True, "ok": True, "warnings": True,
                    "text": "WARNING something"}
    # A runner that returns no report still ends the run.
    assert doctor_report.finished_state(state, None) == doctor_report.idle_state()
    assert doctor_report.failed_state("boom") == {
        "running": False, "ran": True, "ok": False, "warnings": False, "text": "boom"}


def test_doctor_output_is_plain_text() -> None:
    report = parse_output(1, "\x1b[31mERROR\x1b[0m bad\x07\n")
    assert report.text == "ERROR bad" and not report.ok
    assert parse_output(0, "WARNING doctor: Adapter CoD").warnings is True


def test_switch_rows_explain_every_source_and_old_daemons() -> None:
    items = {
        "mark_read_on_dismiss": {"value": True, "source": "settings",
                                 "variable": "BLUEFERRY_MARK_READ_ON_DISMISS",
                                 "restart_required": True},
        "show_notification_content": {"value": True, "source": "environment",
                                      "variable": "BLUEFERRY_SHOW_NOTIFICATION_CONTENT",
                                      "restart_required": False},
    }
    view = features_view.view(features_view.loaded(features_view.initial(), items))
    switches = view["switches"]
    assert switches["mark_read_on_dismiss"] == {
        "usable": True, "active": True, "editable": True,
        "note": "Applies after the service restarts."}
    pinned = switches["show_notification_content"]
    assert pinned["editable"] is False and pinned["active"] is True
    assert "BLUEFERRY_SHOW_NOTIFICATION_CONTENT" in pinned["note"]
    # A switch this daemon does not report names its local.env variable.
    missing = switches["notification_history"]
    assert missing["usable"] is False and missing["editable"] is False
    assert "BLUEFERRY_NOTIFICATION_HISTORY=true or false" in missing["note"]
    assert view["restart_pending"] is True
    assert view["restart_notice"] == "Some changes apply after the BlueFerry service restarts."
    noticed = features_view.view(features_view.with_notice(view, "Restart now"))
    assert noticed["restart_notice"] == "Restart now"
    # Older daemons without GetFeatures: every switch falls back to the hint.
    old = features_view.view(features_view.unavailable(features_view.initial(), "no"))
    assert old["restart_pending"] is False
    assert all(not row["usable"] and "local.env" in row["note"]
               for row in old["switches"].values())
    assert set(old["switches"]) == {
        "show_notification_content", "mark_read_on_dismiss", "notification_history"}


def test_doctor_summary_names_the_worst_outcome() -> None:
    done = {"running": False, "ran": True, "ok": True, "warnings": False, "text": ""}
    assert doctor_report.view(done)["level"] == "ok"
    assert doctor_report.view(done)["summary"] == "All checks passed."
    assert doctor_report.summary({**done, "warnings": True})[0] == "warning"
    assert doctor_report.summary(doctor_report.failed_state("boom")) == (
        "error", "One or more checks failed.")


def test_a_saved_choice_is_assumed_until_the_reload() -> None:
    items = {
        "mark_read_on_dismiss": {"value": False, "running": False, "source": "default",
                                 "restart_required": False},
        "show_notification_content": {"value": True, "running": True,
                                      "source": "environment", "restart_required": False},
    }
    state = features_view.loaded(features_view.initial(), items)
    assumed = features_view.assume(state, "mark_read_on_dismiss", True)
    row = features_view.switch(assumed, "mark_read_on_dismiss")
    assert row["active"] is True and features_view.restart_pending(assumed)
    assert state["items"]["mark_read_on_dismiss"]["value"] is False  # unchanged
    back = features_view.assume(assumed, "mark_read_on_dismiss", False)
    assert not features_view.restart_pending(back)
    # The environment wins: no restart can apply the stored value.
    pinned = features_view.assume(state, "show_notification_content", False)
    assert not features_view.restart_pending(pinned)
    # Unknown switches and old daemons stay as they are.
    assert features_view.assume(state, "notification_history", True) is state


def test_quitting_stops_a_running_doctor_check() -> None:
    import sys
    import threading
    import time

    reports = []
    runner = doctor_report.DoctorRunner()
    worker = threading.Thread(target=lambda: reports.append(runner.run(
        timeout=30, command=[sys.executable, "-c", "import time; time.sleep(30)"])))
    started = time.monotonic()
    worker.start()
    deadline = time.monotonic() + 5
    while not runner._running and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.stop()
    worker.join(5)
    assert not worker.is_alive() and time.monotonic() - started < 10
    assert reports[0].ok is False and not runner._running


def test_a_doctor_check_that_runs_too_long_is_killed() -> None:
    import sys

    report = doctor_report.run_doctor(
        timeout=0.2, command=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert report == DoctorReport(False, False, "blueferry doctor did not finish: TimeoutExpired")
    ok = doctor_report.run_doctor(command=[sys.executable, "-c", "print('WARNING x')"])
    assert ok.ok and ok.warnings and ok.text == "WARNING x"
