"""Inert tests for bounded MAP history queries."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blueferry.obex import map_query
from blueferry.otp_context import OtpMetadata


@pytest.mark.parametrize(
    "matching_props,expected",
    [
        ({"Timestamp": "20261008T184308-0400", "Read": False},
         OtpMetadata(datetime(2026, 10, 8, 22, 43, 8, tzinfo=timezone.utc), False)),
        ({}, OtpMetadata(None, True)),
        ({"Timestamp": "invalid"}, OtpMetadata(None, True)),
        ({"Timestamp": "20261008T224308Z", "Read": True},
         OtpMetadata(datetime(2026, 10, 8, 22, 43, 8, tzinfo=timezone.utc), True)),
        ({"Timestamp": 123456, "Read": False}, OtpMetadata(None, False)),
        ({"Read": None}, OtpMetadata(None, True)),
        ({"Read": "false"}, OtpMetadata(None, True)),
        (None, None),
    ],
)
def test_otp_lookup_matches_the_pushed_message_not_the_newest(
    monkeypatch, matching_props, expected,
):
    session = "/org/bluez/obex/client/session1"
    path = session + "/message1"
    calls = []

    class FakeMap:
        def SetFolder(self, folder, *, timeout):
            calls.append((folder, timeout))

        def ListMessages(self, folder, options, *, timeout):
            calls.append(("list", timeout))
            assert folder == ""
            assert 0 < int(options["MaxListCount"]) <= 20
            messages = {
                session + "/message2": {"Timestamp": "20261008T224400Z"},
            }
            if matching_props is not None:
                messages[path] = matching_props
            return messages

    def obex(requested_path, interface):
        assert requested_path == session
        assert interface == "org.bluez.obex.MessageAccess1"
        return FakeMap()

    monkeypatch.setattr(map_query, "obex", obex)
    monkeypatch.setattr(map_query.time, "monotonic", lambda: 100.0)

    assert map_query.lookup_otp_metadata(session, path) == expected
    assert all(0 < timeout <= 10 for _name, timeout in calls)


def test_otp_lookup_stops_when_the_operation_deadline_expires(monkeypatch):
    clock = [100.0]

    class FakeMap:
        def SetFolder(self, _folder, *, timeout):
            assert timeout <= 10
            clock[0] += 10

        def ListMessages(self, *_args, **_kwargs):
            pytest.fail("expired lookup must not start an inbox listing")

    monkeypatch.setattr(map_query, "obex", lambda *_args: FakeMap())
    monkeypatch.setattr(map_query.time, "monotonic", lambda: clock[0])

    with pytest.raises(TimeoutError):
        map_query.lookup_otp_metadata("/session", "/session/message1")


@pytest.mark.parametrize("path", ["/other/message1", "/session/child/message1", "/session"])
def test_otp_lookup_rejects_other_sessions_before_querying(monkeypatch, path):
    monkeypatch.setattr(
        map_query, "obex", lambda *_args: pytest.fail("must not query another session"),
    )

    assert map_query.lookup_otp_metadata("/session", path) is None


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
