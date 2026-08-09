"""Generative checks for untrusted Bluetooth wire data.

These tests call pure parsers and builders only. The global test harness also
forbids access to the real session and system buses.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

from hypothesis import given, settings
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
from blueferry.obex.map_send import (
    InvalidRecipient,
    build_bmessage,
    validate_recipient,
)

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
