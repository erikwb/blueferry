"""Toolkit-free rules behind the Qt phone card and its Calls and Notifications tabs."""
from __future__ import annotations

from blueferry import phone_overview as overview
from blueferry.models import CallHistoryEntry
from blueferry.qt.phone_link import notification_rows


def test_phone_status_hint_explains_the_opt_in_and_the_wait() -> None:
    assert "Enable phone calls through this computer" in overview.phone_status_hint({})
    assert "oFono" in overview.phone_status_hint(
        {"calls_enabled": True, "calls_state": "unavailable"}
    )
    assert "connecting" in overview.phone_status_hint(
        {"calls_enabled": True, "calls_state": "searching"}
    )
    assert overview.phone_status_hint({"calls_enabled": True, "phone_battery_level": 5}) == ""
    assert overview.phone_status_hint({"calls_enabled": True, "phone_signal_strength": 0}) == ""


def test_proximity_switch() -> None:
    assert not overview.proximity({})["available"]
    enabled = overview.proximity({
        "proximity_lock": "present", "proximity_lock_enabled": True,
        "proximity_lock_grace_sec": 30,
    })
    assert enabled["enabled"] and enabled["grace"] == 30 and "30 s" in enabled["hint"]
    off = overview.proximity({"proximity_lock": "off", "proximity_lock_grace_sec": True})
    assert not off["enabled"] and off["grace"] == 0


def test_history_hints_point_to_their_settings() -> None:
    assert "Keep the iPhone's recent calls" in overview.call_history_hint({})
    assert overview.call_history_hint({"call_history_enabled": True}) == ""
    hint = overview.notifications_hint({})
    assert "Keep recent iPhone notifications" in hint and "restarts" in hint
    assert overview.notifications_hint({"notification_history_enabled": True}) == ""


def test_notification_rows_keep_remote_text_verbatim() -> None:
    rows = notification_rows([
        {"app_name": "Mail", "title": "[b]Hi", "body": "", "subtitle": "<i>sub</i>",
         "time": "2026-08-10T10:00:00+00:00"},
        "garbage",
        {"app_id": "com.example"},
    ])
    assert [r["app"] for r in rows] == ["Mail", "com.example"]
    assert rows[0]["title"] == "[b]Hi" and rows[0]["subtitle"] == "<i>sub</i>"
    assert rows[1]["app"] == "com.example" and rows[1]["body"] == ""
    assert notification_rows(None) == []


def test_call_groups_fold_repeats_and_label_days() -> None:
    from datetime import datetime

    from blueferry.time_display import day_and_clock

    now = datetime(2026, 10, 6, 18, 0)

    def call(direction, when, address="+41790000001", name=None):
        return CallHistoryEntry(direction, when, address, name)

    entries = [
        call("missed", "2026-10-06T14:36:00", name="Anna"),
        call("missed", "2026-10-06T14:30:00", name="Anna"),
        call("missed", "2026-10-06T14:10:00", name="Anna"),
        call("outgoing", "2026-10-06T09:00:00", name="Anna"),
        call("incoming", "2026-10-05T20:15:00", "+41790000002"),
        call("incoming", "2026-10-02T08:00:00", "+41790000002"),
        call("incoming", "2026-09-01T08:00:00", "+41790000002"),
        call("incoming", "2025-12-24T08:00:00", "+41790000002"),
    ]
    rows = overview.call_groups(entries, now=now)
    assert [(row["day"], row["clock"], row["count"]) for row in rows] == [
        ("Today", "2:36 PM", 3), ("Today", "9:00 AM", 1), ("Yesterday", "8:15 PM", 1),
        ("Friday", "8:00 AM", 1), ("Sep 1", "8:00 AM", 1), ("Dec 24, 2025", "8:00 AM", 1),
    ]
    assert rows[0]["missed"] and rows[0]["known"] and rows[0]["caller"] == "Anna"
    assert not rows[2]["known"] and rows[2]["caller"] == "+41790000002"
    missed = overview.call_groups(entries, missed_only=True, now=now)
    assert [row["count"] for row in missed] == [3]
    assert day_and_clock("garbage") == ("", "")
