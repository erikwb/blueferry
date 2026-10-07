"""Generative checks for untrusted Bluetooth wire data.

These tests call pure parsers and builders only. The global test harness also
forbids access to the real session and system buses.
"""
from __future__ import annotations

import io
import struct
from datetime import datetime, timezone

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from blueferry.ancs.constants import CommandID
from blueferry.ancs.parsers import (
    AppAttributes,
    DataSourceAssembler,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
)
from blueferry.contacts import _parse_vcard_records
from blueferry.grouping import correlate_group_events
from blueferry.obex.bmessage import parse as parse_bmessage
from blueferry.obex.map_send import build_bmessage
from blueferry.recipients import InvalidRecipient, validate_recipient
from blueferry.vcard import iter_bounded_lines, iter_vcard_bodies

PROPERTY_SETTINGS = settings(max_examples=150, derandomize=True, deadline=None)
_WIRE_TEXT = st.text(
    alphabet=st.sampled_from(
        list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        + list(" !?.,:;@&+-_()[]{}")
        + ["é", "中", "🙂"],
    ),
    max_size=256,
)


def _attr(attribute_id: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return bytes([attribute_id]) + struct.pack("<H", len(encoded)) + encoded


@PROPERTY_SETTINGS
@given(st.text(max_size=2_048))
def test_arbitrary_text_cannot_crash_bmessage_or_vcard_parsers(blob: str) -> None:
    parsed = parse_bmessage(blob)
    cards = _parse_vcard_records(blob)
    assert parsed.body is None or isinstance(parsed.body, str)
    assert all(len(record) == 3 for record in cards)


_VCARD_LINES = st.lists(
    st.one_of(
        st.sampled_from([
            "BEGIN:VCARD", "END:VCARD", "FN:Ann Lee", "TEL;CELL:+15551234567",
            "EMAIL:a@example.com", "PHOTO;ENCODING=b:QUJD", "QUJDREVG", " QUJD",
            "PHOTO;ENCODING=QUOTED-PRINTABLE:=FF=D8=", "=00:=", "",
            "NOTE;QUOTED-PRINTABLE:a=", "Key;box 12", "KEY:data:x;base64,QUJD",
            "LOGO;VALUE=uri:https://example.invalid/a",
            "NOTE:" + "n" * 400, "PHOTO;QUOTED-PRINTABLE:" + "=FF" * 150 + "=",
            "PHOTO:data:image/png;base64," + "QUJD:" * 80, "QUJD" * 100,
        ]),
        st.text(max_size=48),
    ),
    max_size=24,
)


@PROPERTY_SETTINGS
@given(
    lines=_VCARD_LINES,
    separator=st.sampled_from(["\n", "\r\n", "\r", "\u2028"]),
    limit=st.integers(min_value=1, max_value=64),
    card_limit=st.integers(min_value=0, max_value=256),
)
def test_streamed_vcards_stay_bounded_and_parse_like_the_whole_text(
    lines: list[str], separator: str, limit: int, card_limit: int,
) -> None:
    blob = separator.join(lines)
    streamed = list(iter_vcard_bodies(
        iter_bounded_lines(io.StringIO(blob), limit=limit),
        maximum=100, max_card_chars=card_limit,
    ))
    assert all(len(body) <= card_limit for body in streamed)
    # However the reader cuts lines, the cards match the whole-text parse.
    assert streamed == list(iter_vcard_bodies(blob, maximum=100, max_card_chars=card_limit))


_VALUE_TEXT = st.text(
    alphabet=st.sampled_from(list("abcXYZ019 :;,.\"\\-+/") + ["é", "🙂"]),
    min_size=1,
    max_size=120,
)
_KEPT_HEADS = st.sampled_from([
    "FN", "TEL;TYPE=CELL", "EMAIL;TYPE=INTERNET", "NOTE", "item1.X-ABLabel",
    'ADR;TYPE="home;work"', 'NOTE;X-PHOTO="key:photo"', "NOTE;ENCODING=QUOTED-PRINTABLE",
])


def _not_a_marker(line: str) -> bool:
    return line.strip().casefold() not in {"begin:vcard", "end:vcard"}


@st.composite
def _kept_property(draw) -> tuple[str, list[str]]:
    """A logical property and one valid folding of it into physical lines."""
    head = draw(_KEPT_HEADS)
    value = draw(_VALUE_TEXT)
    logical = f"{head}:{value}"
    cuts = sorted(draw(st.sets(st.integers(1, max(1, len(logical) - 1)), max_size=6)))
    quoted_printable = "QUOTED-PRINTABLE" in head
    if quoted_printable:
        cuts = [cut for cut in cuts if cut > len(head)]  # soft breaks are in the value
    chunks = [logical[start:end] for start, end in zip([0, *cuts], [*cuts, len(logical)], strict=True)]
    chunks = [chunk for chunk in chunks if chunk]
    if quoted_printable:
        if "=" in value:
            return logical, [logical]
        # vCard 2.1 soft line breaks, taken verbatim even after a blank.
        return logical, [chunk + "=" for chunk in chunks[:-1]] + [chunks[-1]]
    fold = draw(st.sampled_from([" ", "\t"]))
    return logical, [chunks[0]] + [fold + chunk for chunk in chunks[1:]]


@st.composite
def _media_property(draw, version21: bool) -> list[str]:
    """Physical lines of a PHOTO, LOGO, SOUND or KEY value of any encoding."""
    name = draw(st.sampled_from(["PHOTO", "LOGO", "SOUND", "item2.KEY", "photo"]))
    size = draw(st.integers(0, 4_000))
    styles = ["b", "qp", "uri", "data"] + (["base64-21"] if version21 else [])
    style = draw(st.sampled_from(styles))
    if style == "qp":
        chunks = ["=FF:=D8" * 5] * (size // 35 + 1)
        lines = [f"{name};ENCODING=QUOTED-PRINTABLE:" + chunks[0]] + chunks[1:]
        return [line + "=" for line in lines[:-1]] + [lines[-1]]
    data = "QUJD" * (size // 4)
    pieces = [data[index:index + 76] for index in range(0, len(data), 76)] or [""]
    if style == "base64-21":
        # The blank line that ends the value is required by vCard 2.1.
        return [f"{name};ENCODING=BASE64;TYPE=JPEG:" + pieces[0], *pieces[1:], ""]
    head = {
        "b": f"{name};ENCODING=b;TYPE=JPEG:",
        "uri": f"{name};VALUE=uri:https://example.invalid/",
        "data": f"{name}:data:image/png;base64,",
    }[style]
    lines = [head + pieces[0]] + [" " + piece for piece in pieces[1:]]
    # vCard 2.1 ends every BASE64 value with a blank line.
    return lines + ([""] if version21 and style == "b" else [])


@PROPERTY_SETTINGS
@given(properties=st.lists(_kept_property(), max_size=8))
def test_unfolding_a_folded_card_restores_every_property(
    properties: list[tuple[str, list[str]]],
) -> None:
    physical = [line for _logical, lines in properties for line in lines]
    assume(all(_not_a_marker(line) for line in physical))
    blob = "\r\n".join(["BEGIN:VCARD", *physical, "END:VCARD"])

    assert list(iter_vcard_bodies(blob, maximum=1)) == [
        "\n".join(logical for logical, _lines in properties),
    ]


@PROPERTY_SETTINGS
@given(
    data=st.data(),
    version=st.sampled_from(["", "VERSION:2.1", "VERSION:3.0"]),
    card_limit=st.integers(min_value=0, max_value=600),
    limit=st.integers(min_value=1, max_value=300),
)
def test_media_values_never_change_the_other_properties(
    data: st.DataObject, version: str, card_limit: int, limit: int,
) -> None:
    entries = data.draw(st.lists(
        st.one_of(
            _kept_property().map(lambda kept: (False, kept[1])),
            _media_property(version == "VERSION:2.1").map(lambda media: (True, media)),
        ),
        max_size=10,
    ))
    lines = [line for _media, block in entries for line in block]
    assume(all(_not_a_marker(line) for line in lines))
    kept = [line for media, block in entries if not media for line in block]

    def bodies(content: list[str], *, streamed: bool) -> list[str]:
        blob = "\r\n".join(["BEGIN:VCARD", version, *content, "END:VCARD", ""])
        source = iter_bounded_lines(io.StringIO(blob, newline=None), limit=limit) if streamed else blob
        return list(iter_vcard_bodies(source, maximum=1, max_card_chars=card_limit))

    expected = bodies(kept, streamed=False)
    assert bodies(lines, streamed=False) == expected
    assert bodies(lines, streamed=True) == expected


@PROPERTY_SETTINGS
@given(st.text(max_size=512))
def test_any_accepted_recipient_has_a_stable_injection_safe_form(raw: str) -> None:
    try:
        normalized = validate_recipient(raw)
    except InvalidRecipient:
        return
    assert "\r" not in normalized and "\n" not in normalized
    assert validate_recipient(normalized) == normalized
    message = build_bmessage(normalized, "safe")
    property_name = "EMAIL" if "@" in normalized else "TEL"
    assert message.count(f"{property_name}:{normalized}\r\n") == 1


@PROPERTY_SETTINGS
@given(_WIRE_TEXT.filter(lambda value: not value or value[:1] != " "))
def test_bmessage_body_round_trips_through_byte_stuffing(body: str) -> None:
    # The parser strips envelope-adjacent trailing spaces, so avoid generating
    # that one inherently ambiguous wire representation here.
    if body.endswith(" "):
        return
    assert parse_bmessage(build_bmessage("+15551234567", body)).body == body


@PROPERTY_SETTINGS
@given(
    app_id=_WIRE_TEXT,
    title=_WIRE_TEXT,
    subtitle=_WIRE_TEXT,
    message=_WIRE_TEXT,
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=17), max_size=40),
)
def test_ancs_attributes_round_trip_across_arbitrary_fragmentation(
    app_id: str,
    title: str,
    subtitle: str,
    message: str,
    chunk_sizes: list[int],
) -> None:
    uid = 0x12345678
    packet = (
        bytes([CommandID.GetNotificationAttributes])
        + struct.pack("<I", uid)
        + _attr(0, app_id)
        + _attr(1, title)
        + _attr(2, subtitle)
        + _attr(3, message)
    )
    assembler = DataSourceAssembler(
        CommandID.GetNotificationAttributes,
        [0, 1, 2, 3],
        notification_id=uid,
    )
    cursor = 0
    complete = None
    for size in chunk_sizes:
        if cursor >= len(packet):
            break
        complete = assembler.feed(packet[cursor:cursor + size])
        cursor += size
    if cursor < len(packet):
        complete = assembler.feed(packet[cursor:])
    assert complete == packet
    parsed = NotificationAttributes.parse(DataSourceEvent.parse(packet).body)
    assert (parsed.app_id, parsed.title, parsed.subtitle, parsed.message) == (
        app_id,
        title,
        subtitle,
        message,
    )


@PROPERTY_SETTINGS
@given(st.binary(max_size=2_048))
def test_malformed_ancs_packets_only_raise_documented_parse_errors(data: bytes) -> None:
    for parser in (
        Notification.parse,
        NotificationAttributes.parse,
        AppAttributes.parse,
        DataSourceEvent.parse,
    ):
        try:
            parser(data)
        except ValueError:
            pass


@PROPERTY_SETTINGS
@given(body=_WIRE_TEXT, sender=st.sampled_from(["Alice", "Bob", "Carol"]))
def test_ambiguous_group_correlation_never_invents_a_reply_route(
    body: str, sender: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    sms = {
        "kind": "sms_received",
        "body": body,
        "seen_at": now,
        "sender_address": "+15551234567",
        "sender_phone_norm": "15551234567",
        "contact_name": sender,
    }
    notification = {
        "kind": "ancs_notification",
        "app_id": "com.apple.MobileSMS",
        "title": sender,
        "subtitle": "To you & Bob",
        "body": body,
        "seen_at": now,
    }
    correlated = correlate_group_events([sms, dict(sms), notification])
    assert all("group_key" not in event for event in correlated[:2])
