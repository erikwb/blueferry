from __future__ import annotations

from blueferry.obex import map_read


def test_session_mark_read_skips_unsafe_handles(monkeypatch) -> None:
    marked = []
    monkeypatch.setattr(map_read, "set_message_read", marked.append)

    map_read.set_session_messages_read(
        "/org/bluez/obex/client/session5",
        ["message1", "../other", "message/evil", ".", "message-ok"],
    )

    assert marked == [
        "/org/bluez/obex/client/session5/message1",
        "/org/bluez/obex/client/session5/message-ok",
    ]
