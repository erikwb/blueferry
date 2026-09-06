"""The toolkit-neutral D-Bus client decodes wire JSON at its boundary."""
from __future__ import annotations

import json

import pytest

from blueferry.client import BackendClient, BackendError
from blueferry.limits import MAX_CONTACT_ADDRESSES_PER_CARD
from blueferry.models import BackendStatus, EventRecord, Thread


class _Messages:
    def IsHealthy(self, **_kwargs):
        return True

    def GetStatus(self, **_kwargs):
        return json.dumps({"daemon": True, "contacts": 3})

    def ListThreads(self, *_args, **_kwargs):
        return json.dumps([{
            "key": "address:email:test@example.com",
            "name": "Test",
            "recipients": ["test@example.com"],
            "reply_ready": True,
            "messages": [],
        }])

    def ListEvents(self, *_args, **_kwargs):
        return json.dumps([{"kind": "ancs_notification", "title": "Test"}])

    def ListContacts(self, *_args, **_kwargs):
        return json.dumps([{
            "name": "Alice Example",
            "phones": ["15551234567"],
            "emails": ["alice@example.com"],
        }])

    def GetNotificationPolicy(self, **_kwargs):
        return "messages"

    def SetNotificationPolicy(self, policy, **_kwargs):
        return policy

    def GetContactsOnlyNotifications(self, **_kwargs):
        return False

    def SetContactsOnlyNotifications(self, enabled, **_kwargs):
        return enabled

    def SetGroupParticipants(self, key, recipients, **_kwargs):
        return json.dumps({
            "key": key,
            "name": "Crew",
            "is_group": True,
            "recipients": list(recipients),
            "reply_ready": True,
            "messages": [],
        })

    def DeleteThreads(self, keys, confirmed, **_kwargs):
        assert bool(confirmed) is True
        return len(keys)

    def MarkThreadRead(self, key, **_kwargs):
        assert key == "address:email:test@example.com"
        return 3


def test_backend_client_returns_shared_models(monkeypatch):
    messages = _Messages()
    client = BackendClient(interface_factory=lambda _name: messages)

    assert client.is_healthy() is True
    assert isinstance(client.status(), BackendStatus)
    assert client.status().contacts == 3
    assert isinstance(client.threads()[0], Thread)
    assert client.threads()[0].recipients == ("test@example.com",)
    assert client.mark_thread_read("address:email:test@example.com") == 3
    assert isinstance(client.events([])[0], EventRecord)
    assert client.events([])[0].title == "Test"
    assert client.list_contacts() == [
        ("Alice Example", ["15551234567"], ["alice@example.com"]),
    ]
    assert client.notification_policy() == "messages"
    assert client.set_notification_policy("none") == "none"
    assert client.contacts_only_notifications() is False
    assert client.set_contacts_only_notifications(True) is True
    group = client.set_group_participants(
        "group:named:test", ["+15551111111", "+15552222222"]
    )
    assert group.reply_ready is True
    assert group.recipients == ("+15551111111", "+15552222222")
    assert client.delete_threads(["one", "two"]) == 2


def test_backend_client_rejects_wrong_json_shape(monkeypatch):
    client = BackendClient()
    messages = _Messages()
    monkeypatch.setattr(messages, "GetStatus", lambda **_kwargs: "[]")
    monkeypatch.setattr(client, "_iface", lambda _name: messages)

    with pytest.raises(BackendError, match="expected dict"):
        client.status()


def test_backend_client_discards_malformed_contact_address_collections(
    monkeypatch,
) -> None:
    client = BackendClient()
    messages = _Messages()
    monkeypatch.setattr(messages, "ListContacts", lambda *_args, **_kwargs: json.dumps([
        {"name": "Alice", "phones": None, "emails": "alice@example.com"},
        {
            "name": "Bob",
            "phones": ["15551234567", None, 42],
            "emails": ["bob@example.com", {"address": "wrong shape"}],
        },
        {
            "name": None,
            "phones": [str(index) for index in range(
                MAX_CONTACT_ADDRESSES_PER_CARD + 1
            )],
            "emails": [],
        },
    ]))
    monkeypatch.setattr(client, "_iface", lambda _name: messages)

    assert client.list_contacts() == [
        ("Alice", [], []),
        ("Bob", ["15551234567"], ["bob@example.com"]),
        (
            "",
            [str(index) for index in range(MAX_CONTACT_ADDRESSES_PER_CARD)],
            [],
        ),
    ]


def test_status_model_normalizes_legacy_map_refusal_detail() -> None:
    status = BackendStatus.from_dict({
        "connectivity_detail": (
            "CreateSession(MAP) failed: Connection refused (111)"
        )
    })

    assert status.map_connection_refused is True
    assert status.to_dict()["map_connection_refused"] is True
