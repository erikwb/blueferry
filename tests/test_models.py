"""Typed client models validate and preserve Messages1 payloads."""

from blueferry import models as models_module
from blueferry.models import BackendStatus, EventRecord, Thread
from blueferry.threads import group_confirmation_token


def test_status_defaults_missing_fields_and_preserves_new_fields():
    status = BackendStatus.from_dict(
        {
            "daemon": True,
            "contacts": "12",
            "notification_policy": "all",
            "contacts_only_notifications": True,
            "storage_policy": "none",
            "storage_state": "disabled",
            "verified_iphone_setup": ["contacts"],
            "connectivity_state": "map-connection-refused",
            "connectivity_detail": "Connection refused (111)",
            "retry_delay_seconds": 15,
            "future_capability": "supported",
        }
    )

    assert status.daemon is True
    assert status.map is False
    assert status.contacts == 12
    assert status.notification_policy == "all"
    assert status.contacts_only_notifications is True
    assert status.storage_policy == "none"
    assert status.storage_state == "disabled"
    assert status.connectivity_state == "map-connection-refused"
    assert status.connectivity_detail == "Connection refused (111)"
    assert status.retry_delay_seconds == 15
    assert status.verified_iphone_setup == ("contacts",)
    assert status.to_dict()["verified_iphone_setup"] == ["contacts"]
    assert status.to_dict()["contacts_only_notifications"] is True
    assert status.extra["future_capability"] == "supported"
    assert status.to_dict()["future_capability"] == "supported"


def test_thread_normalizes_nested_messages(monkeypatch):
    monkeypatch.setattr(
        models_module,
        "format_message_timestamp",
        lambda value: f"friendly:{value}",
    )
    thread = Thread.from_dict(
        {
            "key": "address:phone:15551234567",
            "name": "Alice",
            "recipients": ["+15551234567"],
            "reply_ready": True,
            "group_origin": "named",
            "participants_required": True,
            "roster_changed": True,
            "unexpected_sender": "Casey",
            "prompt_sender": "Alice",
            "roster_warning_id": "route-1:casey",
            "future_thread_field": "preserved",
            "messages": [
                {
                    "handle": "1",
                    "body": "hello",
                    "timestamp": "today",
                    "outgoing": False,
                    "sender": "Alice",
                    "read": True,
                }
            ],
        }
    )

    assert thread.key == "address:phone:15551234567"
    assert thread.messages[0].body == "hello"
    assert thread.messages[0].sender == "Alice"
    assert thread.group_origin == "named"
    assert thread.participants_required is True
    assert thread.roster_changed is True
    assert thread.unexpected_sender == "Casey"
    assert thread.prompt_sender == "Alice"
    assert thread.roster_warning_id == "route-1:casey"
    assert thread.extra["future_thread_field"] == "preserved"
    assert thread.to_dict()["messages"][0]["display_timestamp"] == "friendly:today"
    assert thread.to_dict()["messages"][0]["body"] == "hello"
    assert thread.to_dict()["recipients"] == ["+15551234567"]
    assert thread.to_dict()["prompt_sender"] == "Alice"
    assert thread.to_dict()["roster_warning_id"] == "route-1:casey"
    assert thread.to_dict()["future_thread_field"] == "preserved"
    assert thread.unread is False
    assert thread.to_dict()["unread"] is False
    assert thread.starred is False
    assert thread.to_dict()["starred"] is False
    assert thread.group_confirmed is False
    assert thread.to_dict()["group_confirmed"] is False


def test_thread_confirmation_token_tracks_recipients_and_roster_warning():
    payload = {
        "key": "group:addresses:phone:1|phone:2",
        "name": "Crew",
        "is_group": True,
        "recipients": ["+15552222222", "+15551111111"],
        "roster_warning_id": "route:1:casey",
    }
    thread = Thread.from_dict(payload)

    assert thread.confirmation_token == group_confirmation_token(
        thread.recipients, thread.roster_warning_id
    )
    cleared = Thread.from_dict({**payload, "roster_warning_id": ""})
    assert thread.confirmation_token != cleared.confirmation_token


def test_thread_unread_ignores_outgoing_and_read_incoming():
    unread = Thread.from_dict({
        "key": "address:phone:15550000000",
        "name": "Bob",
        "messages": [
            {
                "handle": "out",
                "body": "hi",
                "timestamp": "today",
                "outgoing": True,
                "read": True,
            },
            {
                "handle": "in",
                "body": "hey",
                "timestamp": "today",
                "outgoing": False,
                "read": False,
            },
        ],
    })

    assert unread.unread is True
    assert unread.to_dict()["unread"] is True


def test_event_record_exposes_common_fields_without_discarding_payload():
    event = EventRecord.from_dict(
        {
            "kind": "ancs_notification",
            "app_id": "com.example",
            "custom": 7,
        }
    )

    assert event.kind == "ancs_notification"
    assert event.app_name == "com.example"
    assert event.data["custom"] == 7
