"""Presentation rules of the GTK settings dialog (no display needed)."""
from __future__ import annotations

import re
from pathlib import Path

from blueferry import doctor_report
from blueferry.doctor_report import DoctorReport
from blueferry.ui.settings_presenter import categories, doctor_rows, feature_subtitle

ROOT = Path(__file__).resolve().parents[1]


def test_categories_follow_the_qt_settings_page() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/SettingsPage.qml").read_text()
    block = qml[qml.index("readonly property var categories"):qml.index("function categoryName")]
    qt = re.findall(r'"key": "(\w+)", "name": qsTr\("([^"]+)"\)', block)
    assert [(c.key, c.title) for c in categories()] == qt
    assert all(c.icon.endswith("-symbolic") for c in categories())


def test_doctor_rows_follow_the_run() -> None:
    idle = doctor_report.idle_state()
    rows = doctor_rows(idle)
    assert not rows.running and not rows.report_shown and rows.run_subtitle == ""
    running = doctor_rows(doctor_report.running_state(idle))
    assert running.running and running.run_subtitle == "Running checks…"
    warned = doctor_report.finished_state(idle, DoctorReport(True, True, "WARNING x"))
    rows = doctor_rows(warned)
    assert rows.report_shown and rows.report_title == "Checks completed with warnings."
    assert (rows.report_icon, rows.report_css) == ("dialog-warning-symbolic", "warning")
    # A second run keeps showing the last report until it ends.
    again = doctor_rows(doctor_report.running_state(warned))
    assert again.running and again.report_shown
    failed = doctor_rows(doctor_report.failed_state("boom"))
    assert failed.report_title == "One or more checks failed." and failed.report_css == "error"
    passed = doctor_rows(doctor_report.finished_state(idle, DoctorReport(True, False, "")))
    assert passed.report_css == "success"


def test_feature_subtitle_joins_description_and_note() -> None:
    assert feature_subtitle("Off: sender only.", "") == "Off: sender only."
    assert feature_subtitle("", "Applies after the service restarts.") == (
        "Applies after the service restarts.")
    assert feature_subtitle("A", "B") == "A\nB"
