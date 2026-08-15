"""Inert MAP download tests; fake interfaces only write local temporary files."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from blueferry.obex import map_events


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def Get(self, destination: str, _attachment: bool, **_kwargs):
        Path(destination).write_bytes(self.payload)
        return "/transfer/test", {"Status": "complete"}


def test_bmessage_download_path_uses_private_runtime_storage(
    tmp_path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    target = map_events._mkstemp_path("blueferry_msg_", ".bmsg")
    try:
        assert target.parent == runtime_dir / "blueferry"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    finally:
        target.unlink(missing_ok=True)


def test_bmessage_download_requires_runtime_storage(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    with pytest.raises(RuntimeError, match="requires XDG_RUNTIME_DIR"):
        map_events._mkstemp_path("blueferry_msg_", ".bmsg")


def test_fetch_rejects_oversized_bmessage_before_parsing(tmp_path, monkeypatch) -> None:
    target = tmp_path / "message.bmsg"
    monkeypatch.setattr(map_events, "MAX_BMESSAGE_BYTES", 16)
    monkeypatch.setattr(
        map_events, "obex", lambda _path, _interface: _Message(b"x" * 17),
    )
    monkeypatch.setattr(map_events, "wait_for_transfer", lambda *_args, **_kwargs: "complete")

    with pytest.raises(RuntimeError, match="safety limit"):
        map_events._fetch_bmessage("/message/test", target)

    assert not target.exists()


def test_fetch_parses_a_completed_local_fixture(tmp_path, monkeypatch) -> None:
    target = tmp_path / "message.bmsg"
    payload = (
        b"BEGIN:BMSG\r\nSTATUS:UNREAD\r\nTYPE:SMS_GSM\r\n"
        b"BEGIN:VCARD\r\nTEL:+15551234567\r\nEND:VCARD\r\n"
        b"BEGIN:MSG\r\nhello\r\nEND:MSG\r\nEND:BMSG\r\n"
    )
    monkeypatch.setattr(
        map_events, "obex", lambda _path, _interface: _Message(payload),
    )
    monkeypatch.setattr(map_events, "wait_for_transfer", lambda *_args, **_kwargs: "complete")

    parsed = map_events._fetch_bmessage("/message/test", target)

    assert parsed.sender_address == "+15551234567"
    assert parsed.body == "hello"
    assert not target.exists()


def test_listener_rejects_messages_from_a_session_with_the_same_prefix() -> None:
    class _Sessions:
        map = type("Session", (), {"path": "/org/bluez/obex/client/session1"})()

    submitted = []
    listener = map_events.MapEventListener(
        _Sessions(),
        lambda _event: None,
        submit_obex=lambda operation, **callbacks: submitted.append(
            (operation, callbacks)
        ),
    )

    listener._on_interfaces_added(
        "/org/bluez/obex/client/session10/message42",
        {"org.bluez.obex.Message1": {}},
    )

    assert submitted == []


def test_failed_body_fetch_does_not_persist_an_unusable_event() -> None:
    class _Sessions:
        map = type("Session", (), {"path": "/session"})()

    received = []
    listener = map_events.MapEventListener(
        _Sessions(), received.append, submit_obex=lambda *_args, **_kwargs: None,
    )
    listener._running = True

    listener._fetch_failed("message42", RuntimeError("transfer failed"))

    assert received == []
