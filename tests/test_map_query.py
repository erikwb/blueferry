"""Inert tests for bounded MAP history queries."""
from __future__ import annotations

import pytest

from blueferry.obex import map_query


def test_remaining_caps_calls_and_rejects_expired_deadline(monkeypatch):
    monkeypatch.setattr(map_query.time, "monotonic", lambda: 100.0)

    assert map_query._remaining(160.0, 10.0) == 10.0
    assert map_query._remaining(105.0, 10.0) == 5.0
    with pytest.raises(TimeoutError, match="operation deadline"):
        map_query._remaining(100.0, 10.0)


def test_recent_query_bounds_every_remote_call(monkeypatch):
    calls: list[tuple[str, float]] = []

    class FakeMap:
        def SetFolder(self, folder, *, timeout):
            calls.append((f"folder:{folder}", timeout))

        def ListMessages(self, _name, _options, *, timeout):
            calls.append(("list", timeout))
            return ["/org/bluez/obex/client/session0/message1"]

    class FakeProperties:
        def GetAll(self, interface, *, timeout):
            assert interface == "org.bluez.obex.Message1"
            calls.append(("properties", timeout))
            return {"Sender": "+15551234567", "Subject": "hello"}

    map_interface = FakeMap()

    def fake_obex(path, interface):
        if interface == "org.bluez.obex.MessageAccess1":
            return map_interface
        assert path.endswith("/message1")
        assert interface == "org.freedesktop.DBus.Properties"
        return FakeProperties()

    monkeypatch.setattr(map_query, "obex", fake_obex)
    monkeypatch.setattr(map_query.time, "monotonic", lambda: 100.0)

    messages = map_query.list_recent_messages(
        "/org/bluez/obex/client/session0", limit=1
    )

    assert messages[0]["body"] == "hello"
    assert [name for name, _timeout in calls] == [
        "folder:/",
        "folder:telecom",
        "folder:msg",
        "folder:INBOX",
        "list",
        "properties",
    ]
    assert all(0 < timeout <= 30 for _name, timeout in calls)


def test_recent_query_bounds_remote_property_sizes(monkeypatch):
    class FakeMap:
        @staticmethod
        def SetFolder(_folder, *, timeout):
            assert timeout > 0

        @staticmethod
        def ListMessages(_name, _options, *, timeout):
            assert timeout > 0
            return ["/session/message1"]

    class FakeProperties:
        @staticmethod
        def GetAll(_interface, *, timeout):
            assert timeout > 0
            return {
                "Sender": "1" * 10_000,
                "Subject": "x" * 100_000,
                "Status": "s" * 10_000,
            }

    monkeypatch.setattr(
        map_query,
        "obex",
        lambda _path, interface: (
            FakeMap()
            if interface == "org.bluez.obex.MessageAccess1"
            else FakeProperties()
        ),
    )

    message = map_query.list_recent_messages("/session", limit=1)[0]

    assert len(message["sender"]) <= map_query.MAX_REMOTE_PROPERTY_CHARS
    assert len(message["body"]) <= map_query.MAX_THREAD_BODY_CHARS
    assert len(message["status"]) <= map_query.MAX_REMOTE_PROPERTY_CHARS
