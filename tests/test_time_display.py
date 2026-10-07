from __future__ import annotations

import locale
import subprocess
from collections.abc import Iterator
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


def _strftime_names_are_english() -> bool:
    # A Thursday afternoon in March: weekday, month and AM/PM all differ in most locales.
    return datetime(2026, 3, 19, 13, 0).strftime("%A %b %p") == "Thursday Mar PM"


def _installed_locale_names() -> list[str]:
    try:
        listing = subprocess.run(
            ["locale", "-a"], capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return listing.stdout.split()


@pytest.fixture
def installed_foreign_time_locale() -> Iterator[str]:
    # GTK and Qt call setlocale(LC_ALL, ""), so a user's LC_TIME reaches strftime.
    # Any installed locale whose strftime names are not English exposes the leak.
    previous = locale.setlocale(locale.LC_TIME)
    try:
        for name in _installed_locale_names():
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            if not _strftime_names_are_english():
                yield name
                return
        pytest.skip("no installed locale has non-English LC_TIME names")
    finally:
        locale.setlocale(locale.LC_TIME, previous)


@pytest.mark.usefixtures("installed_foreign_time_locale")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-09T13:05:00+00:00", "Today at 1:05 PM"),
        ("2026-08-06T09:00:00+00:00", "Thursday at 9:00 AM"),
        ("2026-03-20T18:45:00+00:00", "Mar 20 at 6:45 PM"),
        ("2025-12-31T00:05:00+00:00", "Dec 31, 2025 at 12:05 AM"),
    ],
)
def test_labels_stay_english_under_a_foreign_time_locale(value: str, expected: str) -> None:
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
