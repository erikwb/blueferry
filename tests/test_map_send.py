"""Inert outgoing bMessage construction tests.

Automated tests never call send_message or a live BlueZ OBEX service.
"""
from __future__ import annotations

from pathlib import Path

from blueferry.limits import MAX_OUTGOING_BODY_BYTES
from blueferry.obex import map_send
from blueferry.obex.map_send import (
    _byte_stuff,
    build_bmessage,
    build_group_bmessage,
)


class _MessageAccess:
    def __init__(self) -> None:
        self.source: Path | None = None

    def PushMessage(self, source, _folder, _options, **_kwargs):
        self.source = Path(source)
        assert self.source.read_text(encoding="utf-8").startswith("BEGIN:BMSG")
        return "/transfer/test", {"Status": "complete"}


def test_outgoing_bmessage_uses_runtime_storage_and_is_removed(
    tmp_path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    interface = _MessageAccess()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(map_send, "obex", lambda *_args: interface)
    monkeypatch.setattr(
        map_send, "wait_for_transfer", lambda *_args, **_kwargs: "complete"
    )

    assert map_send.send_message("/session", "+15551234567", "hello") == (
        "/transfer/test"
    )
    assert interface.source is not None
    assert interface.source.parent == runtime_dir / "blueferry"
    assert not interface.source.exists()


class TestByteStuff:
    def test_no_keywords_unchanged(self):
        assert _byte_stuff("hello world") == "hello world"

    def test_begin_line_gets_space_prefix(self):
        assert _byte_stuff("BEGIN:foo") == " BEGIN:foo"

    def test_end_line_gets_space_prefix(self):
        assert _byte_stuff("END:MSG") == " END:MSG"

    def test_keywords_are_case_insensitive(self):
        assert _byte_stuff("end:msg") == " end:msg"

    def test_only_at_line_start(self):
        # "I BEGIN: something" should not get prefixed
        assert _byte_stuff("I BEGIN: something") == "I BEGIN: something"

    def test_multiline_partial(self):
        body = "Hi\nBEGIN:fake\nbye"
        stuffed = _byte_stuff(body)
        # The middle line should be prefixed
        assert "\n BEGIN:fake\n" in stuffed


class TestBuildBmessage:
    def test_basic_map_structure(self):
        bmsg = build_bmessage("+15551234567", "Hello from CI")
        assert "BEGIN:BMSG" in bmsg
        assert "TYPE:SMS_GSM" in bmsg
        assert "FOLDER:telecom/msg/outbox" in bmsg
        assert "TEL:+15551234567" in bmsg
        assert "Hello from CI" in bmsg
        assert "END:BMSG" in bmsg

    def test_has_both_vcards(self):
        # Originator VCARD + BENV-wrapped recipient VCARD
        bmsg = build_bmessage("+15551234567", "x")
        # Two BEGIN:VCARD / END:VCARD pairs
        assert bmsg.count("BEGIN:VCARD") == 2
        assert bmsg.count("END:VCARD") == 2

    def test_apple_id_recipient_uses_email_vcard_property(self):
        bmsg = build_bmessage("person@icloud.com", "hello")
        assert "TYPE:SMS_GSM" in bmsg
        assert "EMAIL:person@icloud.com" in bmsg
        assert "TEL:person@icloud.com" not in bmsg

    def test_recipient_inside_benv(self):
        bmsg = build_bmessage("+15551234567", "x")
        # Sanity check structural ordering
        idx_benv  = bmsg.index("BEGIN:BENV")
        idx_tel   = bmsg.index("TEL:+15551234567")
        idx_bbody = bmsg.index("BEGIN:BBODY")
        assert idx_benv < idx_tel < idx_bbody

    def test_length_matches_body_bytes(self):
        body = "héllo 👋"
        bmsg = build_bmessage("+15551234567", body)
        expected_len = len(body.encode("utf-8"))
        assert f"LENGTH:{expected_len}" in bmsg

    def test_crlf_line_endings(self):
        bmsg = build_bmessage("+15551234567", "hi")
        # The MAP spec wants CRLF
        assert "\r\n" in bmsg
        # And not unexpected bare LFs in the structural lines
        # (header lines should all be terminated with CRLF)
        for header in ("BEGIN:BMSG", "VERSION:1.0", "TYPE:SMS_GSM"):
            assert f"{header}\r\n" in bmsg

    def test_body_with_begin_line_is_stuffed(self):
        # A message body that LITERALLY contains a line starting with
        # "BEGIN:" needs byte-stuffing to not confuse parsers downstream.
        body = "weird message\nBEGIN:trap\nokay"
        bmsg = build_bmessage("+15551234567", body)
        # The stuffed body, between BEGIN:MSG and END:MSG, should have
        # ` BEGIN:trap` (space-prefixed) rather than raw `BEGIN:trap`
        msg_start = bmsg.index("BEGIN:MSG\r\n") + len("BEGIN:MSG\r\n")
        msg_end = bmsg.index("\r\nEND:MSG")
        body_in_bmsg = bmsg[msg_start:msg_end]
        assert " BEGIN:trap" in body_in_bmsg

    def test_body_has_a_utf8_byte_limit(self):
        import pytest

        with pytest.raises(ValueError, match="UTF-8 bytes"):
            build_bmessage("+15551234567", "é" * MAX_OUTGOING_BODY_BYTES)


class TestBuildGroupBmessage:
    def test_two_recipients_share_one_envelope(self):
        bmsg = build_group_bmessage(
            ["person@icloud.com", "+1 (555) 123-4567"], "group hello"
        )

        assert bmsg.count("BEGIN:BMSG") == 1
        assert bmsg.count("BEGIN:BENV") == 1
        assert bmsg.count("BEGIN:VCARD") == 3  # originator + two recipients
        assert "EMAIL:person@icloud.com" in bmsg
        assert "TEL:+15551234567" in bmsg
        assert bmsg.index("EMAIL:person@icloud.com") < bmsg.index("BEGIN:BBODY")
        assert bmsg.index("TEL:+15551234567") < bmsg.index("BEGIN:BBODY")

    def test_requires_two_unique_valid_recipients(self):
        import pytest

        from blueferry.obex.map_send import InvalidRecipient

        with pytest.raises(InvalidRecipient):
            build_group_bmessage(["person@icloud.com"], "x")
        with pytest.raises(InvalidRecipient):
            build_group_bmessage(
                ["person@icloud.com", "person@icloud.com"], "x"
            )
        with pytest.raises(InvalidRecipient):
            build_group_bmessage(
                ["person@icloud.com", "bad\r\nBEGIN:VCARD"], "x"
            )
