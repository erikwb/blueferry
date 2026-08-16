"""Tests for blueferry.obex.bmessage.parse — extracting sender + body
from a MAP bMessage envelope."""
from __future__ import annotations

import textwrap

from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_NAME_CHARS,
    MAX_REMOTE_PROPERTY_CHARS,
)
from blueferry.obex.bmessage import ParsedBMessage, parse


def _bmsg(sender_tel: str, body: str, status: str = "UNREAD") -> str:
    """Build a minimal incoming bMessage for testing."""
    return textwrap.dedent(f"""\
        BEGIN:BMSG
        VERSION:1.0
        STATUS:{status}
        TYPE:SMS_GSM
        FOLDER:telecom/msg/inbox
        BEGIN:VCARD
        VERSION:2.1
        N:Doe;Jane;;;
        FN:Jane Doe
        TEL:{sender_tel}
        END:VCARD
        BEGIN:BENV
        BEGIN:BBODY
        CHARSET:UTF-8
        LENGTH:{len(body)}
        BEGIN:MSG
        {body}
        END:MSG
        END:BBODY
        END:BENV
        END:BMSG
        """).replace("\n", "\r\n")


class TestBasicParsing:
    def test_simple_incoming(self):
        blob = _bmsg("+15551234567", "Hello from the test")
        p = parse(blob)
        assert p.sender_address == "+15551234567"
        assert p.sender_name == "Jane Doe"
        assert p.body == "Hello from the test"
        assert p.status == "UNREAD"
        assert p.type == "SMS_GSM"
        assert p.folder == "telecom/msg/inbox"

    def test_status_read(self):
        blob = _bmsg("+15551234567", "x", status="READ")
        p = parse(blob)
        assert p.status == "READ"

    def test_email_only_sender(self):
        blob = _bmsg("", "hello").replace(
            "TEL:\r\n", "EMAIL:person@icloud.com\r\n")
        p = parse(blob)
        assert p.sender_address == "person@icloud.com"

    def test_tel_is_preferred_when_vcard_has_both(self):
        blob = _bmsg("+15551234567", "hello").replace(
            "TEL:+15551234567\r\n",
            "EMAIL:person@icloud.com\r\nTEL:+15551234567\r\n",
        )
        p = parse(blob)
        assert p.sender_address == "+15551234567"

    def test_empty_body_handled(self):
        blob = _bmsg("+15551234567", "")
        p = parse(blob)
        assert p.body == ""

    def test_no_vcard(self):
        # bMessage without originator VCARD (degenerate but possible)
        blob = (
            "BEGIN:BMSG\r\n"
            "VERSION:1.0\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:5\r\n"
            "BEGIN:MSG\r\n"
            "hello\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        assert p.sender_address is None
        assert p.body == "hello"

    def test_vcard_inside_message_body_cannot_become_sender(self):
        blob = (
            "BEGIN:BMSG\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "BEGIN:MSG\r\n"
            "BEGIN:VCARD\r\n"
            "FN:Planted Name\r\n"
            "TEL:+15550009999\r\n"
            "END:VCARD\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )

        parsed = parse(blob)

        assert parsed.sender_address is None
        assert parsed.sender_name is None
        assert "TEL:+15550009999" in (parsed.body or "")

    def test_recipient_vcard_after_envelope_cannot_become_sender(self):
        blob = (
            "BEGIN:BMSG\nTYPE:SMS_GSM\nBEGIN:BENV\n"
            "BEGIN:VCARD\nFN:Recipient\nTEL:+15550009999\nEND:VCARD\n"
            "BEGIN:BBODY\nBEGIN:MSG\nhello\nEND:MSG\n"
            "END:BBODY\nEND:BENV\nEND:BMSG\n"
        )

        parsed = parse(blob)

        assert parsed.sender_address is None
        assert parsed.sender_name is None


class TestEdgeCases:
    def test_many_unterminated_message_markers_are_scanned_linearly(self):
        parsed = parse("BEGIN:MSG\n" * 10_000)

        assert parsed.body is None

    def test_parser_recovers_from_an_unmatched_indented_message_prefix(self):
        blob = (
            "  BEGIN:MSG\n"
            "malformed prefix\n"
            "BEGIN:MSG\n"
            "hello\n"
            "END:MSG\n"
        )

        assert parse(blob).body == "hello"

    def test_unicode_line_separator_is_body_text_not_structure(self):
        blob = "BEGIN:MSG\nhello\u2028END:MSG\nEND:MSG\n"

        assert parse(blob).body == "hello\nEND:MSG"

    def test_many_unterminated_vcard_markers_are_skipped_linearly(self):
        blob = (
            ("BEGIN:VCARD\n" * 10_000)
            + "BEGIN:VCARD\nFN:Alice\nTEL:+15551234567\nEND:VCARD\n"
            + "BEGIN:BENV\nBEGIN:BBODY\nBEGIN:MSG\nhello\n"
            + "END:MSG\nEND:BBODY\nEND:BENV\n"
        )

        parsed = parse(blob)

        assert parsed.sender_address == "+15551234567"
        assert parsed.sender_name == "Alice"

    def test_remote_metadata_is_bounded(self):
        blob = _bmsg("1" * (MAX_CONTACT_ADDRESS_CHARS + 1), "hello")
        blob = blob.replace(
            "FN:Jane Doe", f"FN:{'N' * (MAX_CONTACT_NAME_CHARS + 10)}"
        ).replace(
            "STATUS:UNREAD", f"STATUS:{'S' * (MAX_REMOTE_PROPERTY_CHARS + 10)}"
        )

        parsed = parse(blob)

        assert parsed.sender_address is None
        assert len(parsed.sender_name or "") == MAX_CONTACT_NAME_CHARS
        assert len(parsed.status or "") == MAX_REMOTE_PROPERTY_CHARS

    def test_multiline_body(self):
        body = "Line 1\nLine 2\nLine 3"
        blob = _bmsg("+15551234567", body)
        p = parse(blob)
        assert p.body == body

    def test_unicode_body(self):
        blob = _bmsg("+15551234567", "héllo 👋 wörld")
        p = parse(blob)
        assert p.body == "héllo 👋 wörld"

    def test_n_field_fallback_when_fn_missing(self):
        # Only N: present, no FN:
        blob = (
            "BEGIN:BMSG\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:VCARD\r\n"
            "N:Smith;John;;;\r\n"
            "TEL:+15551234567\r\n"
            "END:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:2\r\n"
            "BEGIN:MSG\r\n"
            "hi\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        assert p.sender_name == "John Smith"

    def test_tel_with_type_attribute(self):
        blob = (
            "BEGIN:BMSG\r\n"
            "TYPE:SMS_GSM\r\n"
            "BEGIN:VCARD\r\n"
            "FN:Alice\r\n"
            "TEL;TYPE=CELL:+15551234567\r\n"
            "END:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:BBODY\r\n"
            "LENGTH:2\r\n"
            "BEGIN:MSG\r\n"
            "hi\r\n"
            "END:MSG\r\n"
            "END:BBODY\r\n"
            "END:BENV\r\n"
            "END:BMSG\r\n"
        )
        p = parse(blob)
        assert p.sender_address == "+15551234567"
        assert p.sender_name == "Alice"

    def test_garbage_input_doesnt_crash(self):
        for garbage in ["", "not a bmessage", "BEGIN:BMSG\r\nEND:BMSG", "{'json': 'huh'}"]:
            assert parse(garbage) == ParsedBMessage(
                sender_address=None,
                sender_name=None,
                body=None,
                status=None,
                type=None,
                folder=None,
            )
