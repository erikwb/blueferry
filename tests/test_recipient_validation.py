"""Tests for recipient validation — the bMessage injection guard.

A recipient is interpolated into a bMessage vCard property, so anything
that can carry CRLF can forge bMessage structure (e.g. a second recipient
vCard, silently delivering the message to an extra number).
"""
from __future__ import annotations

import pytest

from blueferry.obex.map_send import build_bmessage
from blueferry.recipients import InvalidRecipient, validate_recipient


class TestValidateRecipient:
    @pytest.mark.parametrize("raw,expected", [
        ("+15551234567", "+15551234567"),
        ("15551234567", "15551234567"),
        ("+1 (555) 123-4567", "+15551234567"),
        ("555-1234", "5551234"),
        ("  +15551234567  ", "+15551234567"),
    ])
    def test_accepts_phone_numbers(self, raw, expected):
        assert validate_recipient(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("person@icloud.com", "person@icloud.com"),
        ("Person+messages@ICLOUD.COM", "Person+messages@icloud.com"),
        ("  person@example.net  ", "person@example.net"),
    ])
    def test_accepts_email_addresses(self, raw, expected):
        assert validate_recipient(raw) == expected

    @pytest.mark.parametrize("raw", [
        "+15551234567\r\nEND:VCARD\r\nBEGIN:VCARD\r\nTEL:+19998887777",
        "+15551234567\nTEL:+19998887777",
        "+1555\rEND:VCARD",
        "Mom",
        "",
        "   ",
        "+",
        "12",                       # too short
        "1" * 21,                   # implausibly long
        "+1555123456; DROP TABLE",
        "<b>+15551234567</b>",
        "person@icloud.com\r\nEND:VCARD\r\nBEGIN:VCARD\r\nEMAIL:other@example.com",
        "person@icloud.com\nEMAIL:other@example.com",
        "person@@icloud.com",
        ".person@icloud.com",
        "person..name@icloud.com",
        "person@-icloud.com",
        "person@icloud.com.",
    ])
    def test_rejects_everything_else(self, raw):
        with pytest.raises(InvalidRecipient):
            validate_recipient(raw)

    def test_is_idempotent(self):
        once = validate_recipient("+1 (555) 123-4567")
        assert validate_recipient(once) == once

    def test_email_is_idempotent(self):
        once = validate_recipient("Person@ICLOUD.COM")
        assert validate_recipient(once) == once


class TestBuildBmessageRejectsInjection:
    def test_crlf_recipient_cannot_inject_a_second_vcard(self):
        with pytest.raises(InvalidRecipient):
            build_bmessage(
                "+15551234567\r\nEND:VCARD\r\nBEGIN:VCARD\r\nTEL:+19998887777",
                "hi",
            )

    def test_valid_recipient_appears_exactly_once(self):
        msg = build_bmessage("+1 (555) 123-4567", "hi")
        assert msg.count("BEGIN:VCARD") == 2      # originator + recipient
        assert msg.count("TEL:+15551234567") == 1

    def test_email_recipient_uses_email_property_exactly_once(self):
        msg = build_bmessage("person@icloud.com", "hi")
        assert msg.count("EMAIL:person@icloud.com") == 1
        assert "TEL:person@icloud.com" not in msg
        assert "TYPE:SMS_GSM" in msg

    def test_body_injection_is_still_byte_stuffed(self):
        # The body guard predates this change; confirm it didn't regress.
        msg = build_bmessage("+15551234567", "x\r\nEND:MSG\r\nEND:BMSG")
        # The real terminator is the only unstuffed one.
        assert msg.count("\r\nEND:MSG\r\n") == 1
        assert " END:MSG" in msg
