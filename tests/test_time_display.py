from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blueferry.time_display import format_message_timestamp

NOW = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-09T13:05:00+00:00", "Today at 1:05 PM"),
        ("2026-08-08T23:15:00+00:00", "Yesterday at 11:15 PM"),
        ("2026-08-06T09:00:00+00:00", "Thursday at 9:00 AM"),
        ("2026-07-20T18:45:00+00:00", "Jul 20 at 6:45 PM"),
        ("2025-12-31T00:05:00+00:00", "Dec 31, 2025 at 12:05 AM"),
    ],
)
def test_human_calendar_labels(value: str, expected: str) -> None:
    assert format_message_timestamp(value, now=NOW) == expected


def test_timestamp_is_converted_to_the_reference_timezone() -> None:
    value = "2026-08-08T22:00:00-04:00"

    assert format_message_timestamp(value, now=NOW) == "Today at 2:00 AM"


def test_missing_and_malformed_values_fail_readably() -> None:
    assert format_message_timestamp(None, now=NOW) == ""
    assert format_message_timestamp("not-a-date\nmarkup", now=NOW) == "not-a-date markup"


def test_malformed_timestamp_cannot_emit_terminal_controls_or_new_rows() -> None:
    assert format_message_timestamp("bad\x1b[31m\u2028next", now=NOW) == (
        "bad�[31m�next"
    )
