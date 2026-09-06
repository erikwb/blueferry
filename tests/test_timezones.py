"""Cross-season local dates and mixed-offset history regressions."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from blueferry import events, history, time_display
from blueferry.threads import build_threads


@pytest.fixture(autouse=True)
def summer_in_new_york(monkeypatch):
    import time

    class SummerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            local = cls(2026, 7, 15, 12)
            return local if tz is None else local.astimezone(tz)

    with monkeypatch.context() as patch:
        patch.setenv("TZ", "America/New_York")
        time.tzset()
        for module in (events, history, time_display):
            patch.setattr(module, "datetime", SummerClock)
        try:
            yield
        finally:
            patch.undo()
            time.tzset()


@pytest.mark.parametrize(("month", "offset"), [(1, -5), (7, -4)])
def test_offset_free_map_date_uses_its_own_season(month, offset):
    parsed = events.parse_map_timestamp(f"2026{month:02d}15T120000")
    assert parsed.hour == 12
    assert parsed.utcoffset() == timedelta(hours=offset)


def test_default_display_converts_winter_message_using_winter_offset():
    assert time_display.format_message_timestamp("2026-01-15T17:00:00Z") == "Jan 15 at 12:00 PM"
    # Getting the offset wrong can also shift the calendar date.
    assert time_display.format_message_timestamp("2026-01-16T04:30:00Z") == "Jan 15 at 11:30 PM"


def test_retention_boundary_uses_historical_offset_for_timezone_free_rows(tmp_path):
    path = tmp_path / "events.sqlite"
    cutoff = datetime(2026, 7, 15, 16, tzinfo=timezone.utc) - timedelta(days=180)
    for label, minutes in [("expired", -30), ("retained", 30)]:
        local = (cutoff + timedelta(minutes=minutes)).astimezone(ZoneInfo("America/New_York"))
        history.append_event({
            "kind": "sms_received", "body": label,
            "seen_at": local.replace(tzinfo=None).isoformat(),
        }, path=path)
    assert history.prune_events(path=path, retention_days=180) == 1
    assert [event["body"] for event in history.read_events(path=path)] == ["retained"]


def test_group_correlation_accepts_local_timestamp_alongside_utc():
    threads = build_threads([
        {"kind": "sms_received", "handle": "message-1", "sender_address": "+15551111111",
         "contact_name": "Alice", "body": "hi", "seen_at": "2026-01-15T12:00:00"},
        {"kind": "ancs_notification", "app_id": "com.apple.MobileSMS", "title": "Alice",
         "subtitle": "Family", "body": "hi", "seen_at": "2026-01-15T17:00:01Z"},
    ])
    assert len(threads) == 1
    assert threads[0]["is_group"]
    assert threads[0]["name"] == "Family"
