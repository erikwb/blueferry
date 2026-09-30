"""Parsing and merging of PBAP call-history phonebooks (pure functions only)."""
from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry.call_history import (
    INCOMING,
    MISSED,
    OUTGOING,
    CallRecord,
    canonical_phone_digits,
    display_caller,
    merge_call_history,
    parse_call_history,
    parse_call_timestamp,
    resolve_contact_name,
)
from blueferry.limits import MAX_CONTACT_NAME_CHARS

ZURICH = ZoneInfo("Europe/Zurich")
PROPERTY_SETTINGS = settings(max_examples=150, derandomize=True, deadline=None)


def _card(*lines: str) -> str:
    return "BEGIN:VCARD\nVERSION:2.1\n" + "\n".join(lines) + "\nEND:VCARD\n"


def test_iphone_shaped_missed_call_card() -> None:
    blob = _card(
        "N:Muster;Anna;;;",
        "FN:Anna Muster",
        "TEL;TYPE=CELL:+41 79 555 01 23",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T101500",
    )

    [record] = parse_call_history(blob, folder_direction=MISSED, local_zone=ZURICH)

    assert record.direction == MISSED
    assert record.name == "Anna Muster"
    assert record.address == "+41 79 555 01 23"
    assert record.phone == "41795550123"
    assert record.raw_time == "20260928T101500"
    # Zurich is UTC+2 in September.
    assert record.occurred_at == datetime(2026, 9, 28, 8, 15, tzinfo=timezone.utc)


def test_card_type_parameter_wins_over_folder_and_supports_type_equals() -> None:
    blob = _card(
        "TEL:5551234567",
        "X-IRMC-CALL-DATETIME;TYPE=DIALED:20260928T120000Z",
    ) + _card(
        "TEL:5551234568",
        "X-IRMC-CALL-DATETIME;RECEIVED:20260928T120100Z",
    )

    records = parse_call_history(blob, folder_direction=MISSED)

    assert [record.direction for record in records] == [OUTGOING, INCOMING]


def test_folder_direction_applies_when_card_has_no_type() -> None:
    blob = _card("TEL:5551234567", "X-IRMC-CALL-DATETIME:20260928T120000Z")

    [record] = parse_call_history(blob, folder_direction=OUTGOING)

    assert record.direction == OUTGOING


def test_missing_name_and_withheld_number_are_kept_anonymous() -> None:
    blob = _card("X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z") + _card(
        "FN:", "N:", "TEL:", "X-IRMC-CALL-DATETIME;MISSED:20260928T120500Z",
    )

    records = parse_call_history(blob, folder_direction=MISSED)

    assert [(record.name, record.address, record.phone) for record in records] == [
        (None, "", None), (None, "", None),
    ]
    assert display_caller(records[0], None) is None


def test_structured_name_is_used_when_fn_is_absent() -> None:
    blob = _card(
        "N:Muster;Anna;Maria;;",
        "TEL:+41795550123",
        "X-IRMC-CALL-DATETIME;RECEIVED:20260928T120000Z",
    )

    [record] = parse_call_history(blob, folder_direction=INCOMING)

    assert record.name == "Anna Maria Muster"


def test_phone_number_echoed_as_name_is_not_treated_as_a_name() -> None:
    blob = _card(
        "FN:+41795550123",
        "TEL:+41795550123",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z",
    )

    [record] = parse_call_history(blob, folder_direction=MISSED)

    assert record.name is None
    assert display_caller(record, None) == "+41795550123"
    assert display_caller(record, "Anna") == "Anna"


def test_folded_lines_and_group_prefixes_are_understood() -> None:
    blob = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna\r\n  Muster\r\n"
        "item1.TEL:+4179555\r\n 0123\r\n"
        "X-IRMC-CALL-DATETIME;MISSED:20260928T\r\n 120000Z\r\nEND:VCARD\r\n"
    )

    [record] = parse_call_history(blob, folder_direction=MISSED)

    assert record.name == "Anna Muster"
    assert record.phone == "41795550123"
    assert record.raw_time == "20260928T120000Z"


def test_cards_without_a_usable_timestamp_are_skipped() -> None:
    blob = (
        _card("TEL:5551234567")
        + _card("TEL:5551234567", "X-IRMC-CALL-DATETIME;MISSED:yesterday")
        + _card("TEL:5551234567", "X-IRMC-CALL-DATETIME;MISSED:20261399T250000")
        + _card("TEL:5551234567", "X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z")
    )

    records = parse_call_history(blob, folder_direction=MISSED)

    assert len(records) == 1


def test_remote_controls_are_neutralized_and_names_bounded() -> None:
    blob = _card(
        "FN:Eve\x1b[2J‮" + "x" * (MAX_CONTACT_NAME_CHARS * 2),
        "TEL:+1 555 123 4567",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z",
    )

    [record] = parse_call_history(blob, folder_direction=MISSED)

    assert record.name is not None
    assert "\x1b" not in record.name and "‮" not in record.name
    assert len(record.name) <= MAX_CONTACT_NAME_CHARS


def test_timestamp_formats() -> None:
    utc = datetime(2026, 9, 28, 10, 15, 0, tzinfo=timezone.utc)
    assert parse_call_timestamp("20260928T101500Z") == utc
    assert parse_call_timestamp("20260928T101500z") == utc
    assert parse_call_timestamp("2026-09-28T10:15:00Z") == utc
    assert parse_call_timestamp("20260928T101500.250Z") == utc
    assert parse_call_timestamp("20260928T121500+0200") == utc
    assert parse_call_timestamp("20260928T121500+02:00") == utc
    assert parse_call_timestamp("20260928T051500-05") == utc
    # Offset-free values are the phone's local time on that date (DST aware).
    assert parse_call_timestamp("20260928T121500", local_zone=ZURICH) == utc
    assert parse_call_timestamp("20260115T111500", local_zone=ZURICH) == (
        datetime(2026, 1, 15, 10, 15, tzinfo=timezone.utc)
    )
    for invalid in ("", None, "20260928", "20260928T1015", "20260928T101500+2500",
                    "20260228T101500+0260", "2026-13-01T00:00:00Z", "x" * 100):
        assert parse_call_timestamp(invalid) is None


def _record(direction, stamp, number="15551234567", name=None) -> CallRecord:
    return CallRecord(
        direction=direction,
        occurred_at=parse_call_timestamp(stamp),
        raw_time=stamp,
        address=f"+{number}",
        phone=number,
        name=name,
    )


def test_merge_orders_newest_first_and_collapses_missed_duplicates() -> None:
    received = [
        _record(INCOMING, "20260928T100000Z"),
        _record(INCOMING, "20260928T110000Z", name="Anna"),  # also in mch
    ]
    dialled = [_record(OUTGOING, "20260928T110000Z")]  # same second, other way
    missed = [_record(MISSED, "20260928T110000Z")]

    merged = merge_call_history([received, dialled, missed])

    assert [(record.direction, record.raw_time) for record in merged] == [
        (OUTGOING, "20260928T110000Z"),
        (MISSED, "20260928T110000Z"),
        (INCOMING, "20260928T100000Z"),
    ]
    # The missed copy won, but keeps the name only the received copy carried.
    assert merged[1].name == "Anna"


def test_merge_is_bounded() -> None:
    records = [
        _record(INCOMING, f"20260928T10{minute:02d}00Z") for minute in range(30)
    ]

    merged = merge_call_history([records], maximum=5)

    assert [record.raw_time for record in merged] == [
        f"20260928T10{minute:02d}00Z" for minute in range(29, 24, -1)
    ]


def test_identity_ignores_local_timezone_changes() -> None:
    blob = _card("TEL:5551234567", "X-IRMC-CALL-DATETIME;MISSED:20260928T120000")

    [zurich] = parse_call_history(blob, folder_direction=MISSED, local_zone=ZURICH)
    [tokyo] = parse_call_history(
        blob, folder_direction=MISSED, local_zone=ZoneInfo("Asia/Tokyo"),
    )

    assert zurich.occurred_at != tokyo.occurred_at
    assert zurich.key == tokyo.key


def test_storage_round_trip_and_malformed_rows() -> None:
    record = _record(MISSED, "20260928T120000Z", name="Anna")

    assert CallRecord.from_storage(record.to_storage()) == record
    for malformed in (
        None, [], {}, {**record.to_storage(), "direction": "voicemail"},
        {**record.to_storage(), "occurred_at": "not a date"},
        {**record.to_storage(), "occurred_at": "2026-09-28T12:00:00"},
        {**record.to_storage(), "name": 7},
    ):
        assert CallRecord.from_storage(malformed) is None


# ---- generative ------------------------------------------------------------

@PROPERTY_SETTINGS
@given(st.text(max_size=4_096))
def test_arbitrary_text_cannot_crash_the_call_history_parser(blob: str) -> None:
    for direction in (MISSED, INCOMING, OUTGOING):
        records = parse_call_history(blob, folder_direction=direction)
        assert all(record.occurred_at.tzinfo is not None for record in records)


_FIELD_TEXT = st.text(
    # Exclude everything str.splitlines() treats as a line boundary.
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters="\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029",
    ),
    max_size=80,
)


@PROPERTY_SETTINGS
@given(
    name=st.one_of(st.none(), _FIELD_TEXT),
    telephone=st.one_of(st.none(), _FIELD_TEXT),
    kind=st.sampled_from(["MISSED", "RECEIVED", "DIALED", "", "TYPE=MISSED", "BOGUS"]),
    moment=st.datetimes(
        min_value=datetime(1990, 1, 1), max_value=datetime(2090, 1, 1),
        timezones=st.just(timezone.utc),
    ),
    zone=st.sampled_from(["Z", "+0200", "-05:30", ""]),
)
def test_generated_cards_parse_to_consistent_records(
    name, telephone, kind, moment, zone,
) -> None:
    moment = moment.replace(microsecond=0)
    if zone == "Z":
        stamp = moment.strftime("%Y%m%dT%H%M%S") + "Z"
    elif zone:
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        stamp = (moment + offset).strftime("%Y%m%dT%H%M%S") + zone
    else:
        stamp = moment.strftime("%Y%m%dT%H%M%S")
    lines = []
    if name is not None:
        lines.append(f"FN:{name}")
    if telephone is not None:
        lines.append(f"TEL:{telephone}")
    lines.append(f"X-IRMC-CALL-DATETIME{';' + kind if kind else ''}:{stamp}")

    records = parse_call_history(
        _card(*lines), folder_direction=INCOMING, local_zone=timezone.utc,
    )

    assert len(records) == 1
    record = records[0]
    assert record.occurred_at == moment
    assert record.direction in {MISSED, INCOMING, OUTGOING}
    if kind in {"MISSED", "TYPE=MISSED"}:
        assert record.direction == MISSED
    assert CallRecord.from_storage(record.to_storage()) == record
    assert merge_call_history([records, records]) == records


@PROPERTY_SETTINGS
@given(st.lists(
    st.tuples(
        st.sampled_from([MISSED, INCOMING, OUTGOING]),
        st.integers(min_value=0, max_value=20),
        st.sampled_from(["15551230001", "15551230002"]),
    ),
    max_size=40,
))
def test_merge_never_keeps_two_copies_of_one_call(entries) -> None:
    records = [
        _record(direction, f"20260928T10{minute:02d}00Z", number)
        for direction, minute, number in entries
    ]

    merged = merge_call_history([records])

    keys = [record.merge_key for record in merged]
    assert len(keys) == len(set(keys))
    assert {record.merge_key for record in records} == set(keys)
    assert merged == sorted(
        merged, key=lambda record: (record.occurred_at, record.key), reverse=True,
    )
    for record in merged:
        if record.direction == INCOMING:
            assert all(
                other.direction != MISSED or other.merge_key != record.merge_key
                for other in records
            )


def test_timestamp_text_is_left_intact_for_identity() -> None:
    blob = textwrap.dedent("""\
        BEGIN:VCARD
        TEL:5551234567
        X-IRMC-CALL-DATETIME;MISSED: 20260928T120000Z
        END:VCARD
    """)

    [record] = parse_call_history(blob, folder_direction=MISSED)

    assert record.raw_time == "20260928T120000Z"


# ---- review follow-ups: escapes, quoting, echoed numbers, number spellings ----

def test_vcard_text_escapes_are_resolved_in_n_and_fn() -> None:
    structured = _card(
        r"N:M\;ller;Anna\, Jr.;;;",
        "TEL:+41795550123",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z",
    )
    formatted = _card(
        r"FN:Doe\, John\\Admin\nSecond",
        "TEL:+41795550124",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120100Z",
    )

    [from_n] = parse_call_history(structured, folder_direction=MISSED)
    [from_fn] = parse_call_history(formatted, folder_direction=MISSED)

    assert from_n.name == "Anna, Jr. M;ller"
    # The escaped newline is flattened to one line at the parse boundary.
    assert from_fn.name == "Doe, John\\Admin Second"


def test_colon_inside_a_quoted_parameter_does_not_split_the_value() -> None:
    blob = _card(
        'TEL;X-FOO="a:b";TYPE=CELL:+41 79 555 01 23',
        'X-IRMC-CALL-DATETIME;X-NOTE="x:y";MISSED:20260928T120000Z',
    )

    [record] = parse_call_history(blob, folder_direction=INCOMING)

    assert record.address == "+41 79 555 01 23"
    assert record.phone == "41795550123"
    assert record.direction == MISSED
    assert record.raw_time == "20260928T120000Z"


def test_number_echoed_as_name_in_another_spelling_is_not_a_name() -> None:
    blob = _card(
        "FN:+41 79 123 45 67",
        "TEL:+41791234567",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120000Z",
    ) + _card(
        "FN:0041 79 123 45 68",
        "TEL:+41791234568",
        "X-IRMC-CALL-DATETIME;MISSED:20260928T120100Z",
    )

    records = parse_call_history(blob, folder_direction=MISSED)

    assert [record.name for record in records] == [None, None]


def test_plus_and_double_zero_spellings_are_one_caller() -> None:
    plus = CallRecord(
        direction=MISSED, occurred_at=parse_call_timestamp("20260928T120000Z"),
        raw_time="20260928T120000Z", address="+41 79 555 01 23",
        phone="41795550123", name=None,
    )
    zeros = CallRecord(
        direction=INCOMING, occurred_at=plus.occurred_at,
        raw_time=plus.raw_time, address="0041 79 555 01 23",
        phone="0041795550123", name="Anna",
    )

    [merged] = merge_call_history([[zeros], [plus]])

    assert merged.direction == MISSED and merged.name == "Anna"
    assert plus.number_identity == zeros.number_identity == "41795550123"
    # Contact lookup tries the other international spelling as a fallback.
    assert zeros.resolution_candidates() == ["0041795550123", "+41795550123"]
    assert plus.resolution_candidates() == ["41795550123", "0041795550123"]
    # Short numbers starting with 00 are left alone.
    assert canonical_phone_digits("0012345") == "0012345"


def test_contact_resolution_uses_the_first_spelling_that_matches() -> None:
    record = CallRecord(
        direction=MISSED, occurred_at=parse_call_timestamp("20260928T120000Z"),
        raw_time="20260928T120000Z", address="0041 79 555 01 23",
        phone="0041795550123", name=None,
    )
    asked = []

    def resolve(address):
        asked.append(address)
        return "Anna" if address == "+41795550123" else None

    assert resolve_contact_name(record, resolve) == "Anna"
    assert asked == ["0041795550123", "+41795550123"]
