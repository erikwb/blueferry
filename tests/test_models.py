"""Typed client models validate and preserve Messages1 payloads."""
from blueferry.models import BackendStatus, EventRecord, Thread


def test_status_defaults_missing_fields_and_preserves_new_fields():
    status = BackendStatus.from_dict({
        "daemon": True,
        "contacts": "12",
        "notification_policy": "all",
        "storage_policy": "none",
        "storage_state": "disabled",
        "future_capability": "supported",
    })

    assert status.daemon is True
    assert status.map is False
    assert status.contacts == 12
    assert status.notification_policy == "all"
    assert status.storage_policy == "none"
    assert status.storage_state == "disabled"
    assert status.extra["future_capability"] == "supported"
    assert status.to_dict()["future_capability"] == "supported"


def test_thread_normalizes_nested_messages():
    thread = Thread.from_dict({
        "key": "address:phone:15551234567",
        "name": "Alice",
        "recipients": ["+15551234567"],
        "reply_ready": True,
        "messages": [{
            "handle": "1", "body": "hello", "timestamp": "today",
            "outgoing": False, "read": True,
        }],
    })

    assert thread.key == "address:phone:15551234567"
    assert thread.messages[0].body == "hello"
    assert thread.to_dict()["messages"][0]["body"] == "hello"
    assert thread.to_dict()["recipients"] == ["+15551234567"]


def test_event_record_exposes_common_fields_without_discarding_payload():
    event = EventRecord.from_dict({
        "kind": "ancs_notification", "app_id": "com.example", "custom": 7,
    })

    assert event.kind == "ancs_notification"
    assert event.app_name == "com.example"
    assert event.data["custom"] == 7
