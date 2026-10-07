from __future__ import annotations

import locale
import os
import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

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


# A locale of our own, so the test does not depend on which locales a host or
# CI image happens to ship. Only LC_TIME differs from en_US; like de_CH it has
# an empty AM/PM marker. Arch's container image keeps the en_US and UTF-8
# sources that localedef needs here but strips every non-English one.
_GENERATED_LOCALE = "bf_TEST.UTF-8"
_GENERATED_LOCALE_SOURCE = "\n".join(
    [
        *(
            f'{category}\ncopy "en_US"\nEND {category}'
            for category in (
                "LC_IDENTIFICATION",
                "LC_CTYPE",
                "LC_COLLATE",
                "LC_MONETARY",
                "LC_NUMERIC",
                "LC_MESSAGES",
                "LC_PAPER",
                "LC_NAME",
                "LC_ADDRESS",
                "LC_TELEPHONE",
                "LC_MEASUREMENT",
            )
        ),
        "LC_TIME",
        'abday "So";"Mo";"Di";"Mi";"Do";"Fr";"Sa"',
        'day "Sonntag";"Montag";"Dienstag";"Mittwoch";"Donnerstag";"Freitag";"Samstag"',
        'abmon "Jan";"Feb";"Mrz";"Apr";"Mai";"Jun";"Jul";"Aug";"Sep";"Okt";"Nov";"Dez"',
        'mon "Januar";"Februar";"Maerz";"April";"Mai";"Juni";"Juli";"August";'
        '"September";"Oktober";"November";"Dezember"',
        'd_t_fmt "%a %d %b %Y %T"',
        'd_fmt "%d.%m.%Y"',
        't_fmt "%T"',
        'am_pm "";""',
        't_fmt_ampm ""',
        "END LC_TIME",
        "",
    ]
)
# CI sets this so a missing localedef fails the run instead of skipping the
# regression test unnoticed.
_REQUIRE_GENERATED_LOCALE = "BLUEFERRY_REQUIRE_LOCALE_TEST"


def _generated_locale_unavailable(reason: str) -> None:
    if os.environ.get(_REQUIRE_GENERATED_LOCALE):
        pytest.fail(f"{reason} ({_REQUIRE_GENERATED_LOCALE} is set)")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def generated_locale_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("locale")
    source = root / "bf_TEST"
    source.write_text(_GENERATED_LOCALE_SOURCE, encoding="utf-8")
    try:
        subprocess.run(
            ["localedef", "-i", str(source), "-f", "UTF-8", str(root / _GENERATED_LOCALE)],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip()
        _generated_locale_unavailable(f"localedef could not build a test locale: {detail}")
    except (OSError, subprocess.SubprocessError) as error:
        _generated_locale_unavailable(f"localedef could not build a test locale: {error}")
    return root


@pytest.fixture
def generated_foreign_time_locale(generated_locale_path: Path) -> Iterator[str]:
    previous = locale.setlocale(locale.LC_TIME)
    previous_path = os.environ.get("LOCPATH")
    # glibc reads LOCPATH on every setlocale() call.
    os.environ["LOCPATH"] = str(generated_locale_path)
    try:
        try:
            locale.setlocale(locale.LC_TIME, _GENERATED_LOCALE)
        except locale.Error as error:
            _generated_locale_unavailable(f"the generated test locale does not load: {error}")
        assert not _strftime_names_are_english()
        yield _GENERATED_LOCALE
    finally:
        # Restore LOCPATH first: while it is set glibc ignores the system
        # locale archive, so the previous locale might not load.
        if previous_path is None:
            os.environ.pop("LOCPATH", None)
        else:
            os.environ["LOCPATH"] = previous_path
        locale.setlocale(locale.LC_TIME, previous)


@pytest.mark.parametrize(
    "time_locale", ["installed_foreign_time_locale", "generated_foreign_time_locale"]
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-09T13:05:00+00:00", "Today at 1:05 PM"),
        ("2026-08-06T09:00:00+00:00", "Thursday at 9:00 AM"),
        ("2026-03-20T18:45:00+00:00", "Mar 20 at 6:45 PM"),
        ("2025-12-31T00:05:00+00:00", "Dec 31, 2025 at 12:05 AM"),
    ],
)
def test_labels_stay_english_under_a_foreign_time_locale(
    request: pytest.FixtureRequest, time_locale: str, value: str, expected: str
) -> None:
    request.getfixturevalue(time_locale)

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
