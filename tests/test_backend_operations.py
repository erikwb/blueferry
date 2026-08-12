"""Backend trust-boundary behavior independent of its D-Bus transport."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import backend_operations
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.errors import (
    ConfirmationRequiredError,
    InvalidArgumentsError,
    OperationFailedError,
)
from blueferry.limits import (
    MAX_EVENT_QUERY_LIMIT,
    MAX_OUTGOING_BODY_BYTES,
    MAX_THREAD_BODY_CHARS,
)


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error):
        pass


def _operations(**dependencies) -> BackendOperations:
    def submit(operation, *, on_success, on_error):
        try:
            on_success(operation())
        except Exception as error:
            on_error(error)

    dependencies.setdefault("submit_obex", submit)
    return BackendOperations(_Sessions(), BackendDependencies(**dependencies))


def _group() -> dict:
    return {
        "key": "group:addresses:phone:1|phone:2",
        "name": "Alice, Bob",
        "is_group": True,
        "members": ["Alice", "Bob"],
        "recipients": ["+15551111111", "+15552222222"],
        "reply_ready": True,
    }


def _stub_group(operations: BackendOperations, thread: dict) -> None:
    operations._conversations.threads = lambda: [thread]


def test_group_reply_requires_confirmation_before_send(monkeypatch):
    operations = _operations()
    thread = _group()
    _stub_group(operations, thread)

    with pytest.raises(ConfirmationRequiredError):
        operations.send_to_thread(
            thread["key"], "hello", False,
            lambda _result: pytest.fail("unexpected successful reply"),
            lambda error: pytest.fail(str(error)),
        )


def test_confirmed_group_reply_uses_backend_recipient_set(monkeypatch):
    operations = _operations()
    thread = _group()
    sent = []
    _stub_group(operations, thread)
    monkeypatch.setattr(
        backend_operations,
        "send_group_message",
        lambda _path, recipients, body: sent.append((list(recipients), body))
        or "/transfer/1",
    )

    replies = []
    operations.send_to_thread(
        thread["key"], "hello", True, replies.append,
        lambda error: pytest.fail(str(error)),
    )

    assert replies == ["/transfer/1"]
    assert sent == [(["+15551111111", "+15552222222"], "hello")]
    assert thread["key"] in operations._confirmed_group_keys


def test_group_reply_records_the_projected_member_roster(monkeypatch):
    recorded = []
    operations = _operations(
        on_group_sent=lambda *values: recorded.append(values)
    )
    thread = _group()
    _stub_group(operations, thread)
    monkeypatch.setattr(
        backend_operations,
        "send_group_message",
        lambda *_args: "/transfer/2",
    )

    operations.send_to_thread(
        thread["key"], "hello", True,
        lambda _result: None,
        lambda error: pytest.fail(str(error)),
    )

    assert recorded[0][-1] == ["Alice", "Bob"]


def test_failed_group_reply_is_not_marked_confirmed(monkeypatch):
    operations = _operations()
    thread = _group()
    _stub_group(operations, thread)
    monkeypatch.setattr(
        backend_operations,
        "send_group_message",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("phone left")),
    )
    errors = []

    operations.send_to_thread(
        thread["key"], "hello", True,
        lambda _result: pytest.fail("unexpected successful reply"),
        errors.append,
    )

    assert len(errors) == 1
    assert isinstance(errors[0], OperationFailedError)
    assert thread["key"] not in operations._confirmed_group_keys


def test_notification_policy_is_backend_owned_and_notifies_status() -> None:
    class Policy:
        value = "messages"

        def set(self, value):
            self.value = value
            return value

    changes = []
    operations = _operations(
        notification_policy=Policy(),
        on_notification_policy_changed=lambda: changes.append(True),
    )

    assert operations.get_notification_policy() == "messages"
    assert operations.set_notification_policy("all") == "all"
    assert operations.get_notification_policy() == "all"
    assert changes == [True]


def test_invalid_notification_policy_has_public_invalid_args_error() -> None:
    class Policy:
        value = "messages"

        @staticmethod
        def set(_value):
            raise ValueError("bad policy")

    operations = _operations(notification_policy=Policy())

    with pytest.raises(InvalidArgumentsError) as caught:
        operations.set_notification_policy("bad")

    assert getattr(caught.value, "dbus_suffix", None) == "InvalidArgs"


def test_storage_policy_change_clears_data_before_switching(monkeypatch) -> None:
    calls = []

    class Storage:
        status = SimpleNamespace(policy="encrypted", can_read=True)

        def set_policy(self, value, *, allow_prompt):
            assert allow_prompt is True
            calls.append(("set", value))
            self.status = SimpleNamespace(
                policy=value,
                state="ready",
                detail="Local data is retained without encryption",
                can_read=True,
            )
            return self.status

    monkeypatch.setattr(
        backend_operations, "clear_events", lambda: calls.append("events")
    )
    monkeypatch.setattr(
        backend_operations, "clear_contact_cache", lambda: calls.append("contacts")
    )
    operations = BackendOperations(
        _Sessions(), BackendDependencies(storage=Storage())
    )

    result = operations.set_storage_policy("plaintext")

    assert calls == ["events", "contacts", ("set", "plaintext")]
    assert result["storage_policy"] == "plaintext"


def test_invalid_storage_policy_does_not_clear_data(monkeypatch) -> None:
    cleared = []
    storage = SimpleNamespace(status=SimpleNamespace(policy="encrypted"))
    operations = BackendOperations(
        _Sessions(), BackendDependencies(storage=storage)
    )
    monkeypatch.setattr(
        backend_operations, "clear_events", lambda: cleared.append("events")
    )
    monkeypatch.setattr(
        backend_operations,
        "clear_contact_cache",
        lambda: cleared.append("contacts"),
    )

    with pytest.raises(InvalidArgumentsError, match="local data policy"):
        operations.set_storage_policy("surprise")

    assert cleared == []


def test_outgoing_body_limit_is_enforced_before_obex() -> None:
    operations = _operations()

    with pytest.raises(InvalidArgumentsError, match="UTF-8 bytes"):
        operations.send(
            "+15551234567", "é" * MAX_OUTGOING_BODY_BYTES,
            lambda _result: pytest.fail("unexpected send"),
            lambda error: pytest.fail(str(error)),
        )


def test_map_folder_rejects_parent_navigation() -> None:
    operations = _operations()

    with pytest.raises(InvalidArgumentsError, match="folder"):
        operations.list_recent(
            "telecom/msg/../outbox", 20,
            lambda _result: pytest.fail("unexpected query"),
            lambda error: pytest.fail(str(error)),
        )


def test_contact_lookup_stays_behind_backend_boundary() -> None:
    class _Contacts:
        @staticmethod
        def find_by_name(query):
            assert query == "Alice"
            return [("Alice Example", "15551234567")]

    operations = _operations(contacts=_Contacts())

    assert operations.find_contacts("Alice") == [{
        "name": "Alice Example",
        "address": "15551234567",
    }]


def test_history_snapshot_validates_kinds_and_caps_bodies(monkeypatch) -> None:
    captured = {}

    def read_events(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(backend_operations, "read_events", read_events)
    operations = _operations()

    assert operations.list_events(["sms_received"], 50_000) == []
    assert captured["limit"] == MAX_EVENT_QUERY_LIMIT
    assert captured["max_body_chars"] == MAX_THREAD_BODY_CHARS

    operations.list_events([], 10)
    assert "ancs_notification" not in captured["kinds"]

    with pytest.raises(InvalidArgumentsError, match="event kind"):
        operations.list_events(["../../private"], 10)

    with pytest.raises(InvalidArgumentsError, match="event kind"):
        operations.list_events(["ancs_notification"], 10)
