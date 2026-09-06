"""Tests for blueferry.events — phone normalization, timestamp parsing,
SmsEvent construction from MAP Message1 properties."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from blueferry.events import (
    normalize_phone,
    parse_map_timestamp,
    sms_group_sent_event,
    sms_sent_event,
)


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+1 (555) 123-4567", "15551234567"),
        ("+15551234567",      "15551234567"),
        ("5551234567",        "5551234567"),
        ("(555) 123-4567",    "5551234567"),
        ("555.123.4567",      "5551234567"),
        ("555 123 4567",      "5551234567"),
        ("+1-555-123-4567",   "15551234567"),
    ])
    def test_real_numbers_pass(self, raw, expected):
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "Mom", "123", "abcdef"])
    def test_non_phones_return_none(self, raw):
        assert normalize_phone(raw) is None

    def test_short_digit_string_returns_none(self):
        # 6 digits is below threshold
        assert normalize_phone("555-1234") == "5551234"  # 7 digits is the boundary
        assert normalize_phone("12345") is None          # 5 digits is too short

    def test_email_digits_are_not_mistaken_for_a_phone(self):
        assert normalize_phone("person1234567@icloud.com") is None

    def test_phone_plus_untrusted_text_is_not_normalized(self):
        assert normalize_phone("+15551234567Mom") is None


class TestParseMapTimestamp:
    def test_basic_format(self):
        result = parse_map_timestamp("20260519T181423")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 19
        assert result.hour == 18
        assert result.minute == 14
        assert result.second == 23

    @pytest.mark.parametrize(("suffix", "hours"), [("+0500", 5), ("-0400", -4), ("Z", 0)])
    def test_with_tz_suffix(self, suffix, hours):
        result = parse_map_timestamp(f"20260519T181423{suffix}")
        assert result is not None
        assert result.hour == 18
        assert result.utcoffset() == timedelta(hours=hours)

    @pytest.mark.parametrize("bad", ["", None, "not a date", "20260", "abcdef", "20260519T181423+2500",
                                     "20260519T181423garbage"])
    def test_invalid_returns_none(self, bad):
        assert parse_map_timestamp(bad) is None


class TestSmsSentEvent:
    def test_recipient_lands_in_address_fields(self):
        # A sent event carries the recipient in address fields so it threads with
        # incoming messages from the same person.
        e = sms_sent_event("+15551234567", "on my way",
                           contact_name="Alice",
                           transfer_path="/org/bluez/obex/client/session0/transfer3")
        assert e.kind == "sms_sent"
        assert e.sender_address == "+15551234567"
        assert e.sender_phone_norm == "15551234567"
        assert e.contact_name == "Alice"
        assert e.body == "on my way"
        assert e.is_read is True
        assert e.handle == "transfer3"
        assert e.timestamp is not None
        assert e.display_sender == "Alice"

    def test_handle_synthesized_without_transfer_path(self):
        first = sms_sent_event("+15551234567", "hi")
        second = sms_sent_event("+15551234567", "hi")
        assert first.handle.startswith("sent-")
        assert first.handle != second.handle

    def test_group_sent_event_serializes_thread_metadata(self):
        event = sms_group_sent_event(
            ["person@icloud.com", "+15551234567"],
            "hello group",
            group_key="group:participants:alice|bob",
            group_name="Alice, Bob",
            group_members=["Alice", "Bob"],
            transfer_path="/org/bluez/obex/client/session0/transfer9",
        )
        payload = event.to_dict()
        assert event.display_sender == "Alice, Bob"
        assert payload["group_recipients"] == [
            "person@icloud.com", "+15551234567"
        ]
        assert payload["group_reply_ready"] is True
        assert payload["group_members"] == ["Alice", "Bob"]
        assert payload["handle"] == "transfer9"
