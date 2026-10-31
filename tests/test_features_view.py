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
