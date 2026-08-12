"""The toolkit-neutral D-Bus client decodes wire JSON at its boundary."""
from __future__ import annotations

import json

import pytest

from blueferry.client import BackendClient, BackendError
from blueferry.models import BackendStatus, EventRecord, Thread


class _Messages:
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

    def GetNotificationPolicy(self, **_kwargs):
        return "messages"

    def SetNotificationPolicy(self, policy, **_kwargs):
        return policy


def test_backend_client_returns_shared_models(monkeypatch):
    client = BackendClient()
    monkeypatch.setattr(client, "_iface", lambda _name: _Messages())

    assert isinstance(client.status(), BackendStatus)
    assert client.status().contacts == 3
    assert isinstance(client.threads()[0], Thread)
    assert client.threads()[0].recipients == ("test@example.com",)
    assert isinstance(client.events([])[0], EventRecord)
    assert client.events([])[0].title == "Test"
    assert client.notification_policy() == "messages"
    assert client.set_notification_policy("none") == "none"


def test_backend_client_rejects_wrong_json_shape(monkeypatch):
    client = BackendClient()
    messages = _Messages()
    monkeypatch.setattr(messages, "GetStatus", lambda **_kwargs: "[]")
    monkeypatch.setattr(client, "_iface", lambda _name: messages)

    with pytest.raises(BackendError, match="expected dict"):
        client.status()


def test_status_model_normalizes_legacy_map_refusal_detail() -> None:
    status = BackendStatus.from_dict({
        "connectivity_detail": (
            "CreateSession(MAP) failed: Connection refused (111)"
        )
    })

    assert status.map_connection_refused is True
    assert status.to_dict()["map_connection_refused"] is True
