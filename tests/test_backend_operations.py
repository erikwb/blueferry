"""Backend trust-boundary behavior independent of its D-Bus transport."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import backend_operations, config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.errors import (
    ConfirmationRequiredError,
    InvalidArgumentsError,
    NotFoundError,
    NotReadyError,
    OperationFailedError,
)
from blueferry.grouping import named_group_key
from blueferry.history import append_event, read_events
from blueferry.limits import (
    MAX_CONTACT_PAGE,
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


def test_named_group_requires_confirmation_for_every_reply(monkeypatch):
    operations = _operations()
    thread = {**_group(), "group_origin": "named"}
    _stub_group(operations, thread)
    monkeypatch.setattr(
        backend_operations,
        "send_group_message",
        lambda *_args: "/transfer/named",
    )

    operations.send_to_thread(
        thread["key"], "first", True,
        lambda _result: None,
        lambda error: pytest.fail(str(error)),
    )

    assert thread["key"] not in operations._confirmed_group_keys
    with pytest.raises(ConfirmationRequiredError):
        operations.send_to_thread(
            thread["key"], "second", False,
            lambda _result: pytest.fail("unexpected successful reply"),
            lambda error: pytest.fail(str(error)),
        )


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


def test_contact_enumeration_keeps_one_person_as_one_record() -> None:
    class _Contacts:
        @staticmethod
        def records(offset, limit):
            assert (offset, limit) == (0, MAX_CONTACT_PAGE)
            return [("Alice Example", ["15551234567"], ["alice@example.com"])]

    operations = _operations(contacts=_Contacts())

    assert operations.list_contacts(0, MAX_CONTACT_PAGE) == [{
        "name": "Alice Example",
        "phones": ["15551234567"],
        "emails": ["alice@example.com"],
    }]


def test_contact_page_is_bounded_and_offset_cannot_go_negative() -> None:
    seen: list[tuple[int, int]] = []

    class _Contacts:
        @staticmethod
        def records(offset, limit):
            seen.append((offset, limit))
            return []

    operations = _operations(contacts=_Contacts())
    operations.list_contacts(-5, MAX_CONTACT_PAGE * 10)

    assert seen == [(0, MAX_CONTACT_PAGE)]


def test_contact_enumeration_without_a_cache_is_not_ready() -> None:
    operations = _operations(contacts=None)

    with pytest.raises(NotReadyError):
        operations.list_contacts(0, 10)


def test_named_group_roster_is_validated_and_persisted(monkeypatch) -> None:
    class Contacts:
        @staticmethod
        def resolve(address):
            return {
                "+15551111111": "Beau",
                "+15552222222": "Alice",
            }.get(address)

    operations = _operations(contacts=Contacts())
    provisional = {
        "key": "group:named:test",
        "name": "Crew",
        "is_group": True,
        "group_origin": "named",
        "observed_recipients": ["+15551111111"],
        "recipients": ["+15551111111"],
        "reply_ready": False,
    }
    updated = {**provisional, "reply_ready": True}
    retained = []
    monkeypatch.setattr(
        operations._conversations,
        "find",
        lambda _key: updated if retained else provisional,
    )
    monkeypatch.setattr(
        backend_operations,
        "append_event",
        lambda event, **_kwargs: retained.append(event),
    )

    result = operations.set_group_participants(
        provisional["key"], ["+1 (555) 111-1111", "+15552222222"]
    )

    assert result["reply_ready"] is True
    assert retained[0]["group_name"] == "Crew"
    assert retained[0]["group_members"] == ["Beau", "Alice"]
    assert retained[0]["group_recipients"] == [
        "+15551111111", "+15552222222"
    ]


def test_named_group_roster_cannot_omit_an_observed_sender(monkeypatch) -> None:
    operations = _operations()
    thread = {
        "key": "group:named:test",
        "name": "Crew",
        "is_group": True,
        "group_origin": "named",
        "observed_recipients": ["+15551111111"],
        "recipients": ["+15551111111"],
        "reply_ready": False,
    }
    monkeypatch.setattr(operations._conversations, "find", lambda _key: thread)

    with pytest.raises(InvalidArgumentsError, match="observed sender"):
        operations.set_group_participants(
            thread["key"], ["+15552222222", "+15553333333"]
        )


def test_named_group_key_survives_roster_save_and_history_reload(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    key = named_group_key("Crew")
    append_event({
        "kind": "sms_received",
        "handle": "message-1",
        "sender_address": "+15551111111",
        "contact_name": "Beau",
        "body": "hello",
        "seen_at": "2026-08-12T10:00:00+00:00",
    })
    append_event({
        "kind": "ancs_notification",
        "notification_id": 42,
        "app_id": "com.apple.MobileSMS",
        "title": "Beau",
        "subtitle": "Crew",
        "body": "hello",
        "seen_at": "2026-08-12T10:00:23+00:00",
    })
    operations = _operations()

    assert operations.list_threads(10)[0]["key"] == key
    updated = operations.set_group_participants(
        key, ["+15551111111", "+15552222222"]
    )

    assert updated["key"] == key
    assert updated["reply_ready"] is True
    assert updated["participants_required"] is False
    assert updated["roster_changed"] is False


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


def test_delete_threads_erases_selected_conversation_and_notification_evidence(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "events.sqlite"
    monkeypatch.setattr(config, "EVENTS_DB", path)
    append_event({
        "kind": "sms_received",
        "handle": "alice-message",
        "sender_address": "+15551111111",
        "contact_name": "Alice",
        "body": "private alice text",
        "seen_at": "2026-08-12T10:00:00+00:00",
    }, path=path)
    append_event({
        "kind": "ancs_notification",
        "notification_id": 1,
        "app_id": "com.apple.MobileSMS",
        "title": "Alice",
        "subtitle": "",
        "body": "private alice text",
        "seen_at": "2026-08-12T10:00:02+00:00",
    }, path=path)
    append_event({
        "kind": "sms_seen",
        "handle": "alice-message",
        "sender_address": "+15551111111",
        "contact_name": "Alice",
        "body": "private alice text",
        "seen_at": "2026-08-12T10:00:03+00:00",
    }, path=path)
    append_event({
        "kind": "sms_received",
        "handle": "bob-message",
        "sender_address": "+15552222222",
        "contact_name": "Bob",
        "body": "retained bob text",
        "seen_at": "2026-08-12T10:01:00+00:00",
    }, path=path)
    operations = _operations()
    alice = next(
        thread for thread in operations.list_threads(10)
        if thread["name"] == "Alice"
    )

    assert operations.delete_threads([alice["key"]], True) == 1

    retained = read_events(path=path)
    assert [event["body"] for event in retained] == ["retained bob text"]
    assert [thread["name"] for thread in operations.list_threads(10)] == ["Bob"]


def test_delete_threads_erases_named_group_route_and_matched_ancs(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "events.sqlite"
    monkeypatch.setattr(config, "EVENTS_DB", path)
    key = named_group_key("Crew")
    append_event({
        "kind": "group_route",
        "group_key": key,
        "group_name": "Crew",
        "group_members": ["Beau", "Alice"],
        "group_recipients": ["+15551111111", "+15552222222"],
        "seen_at": "2026-08-12T09:00:00+00:00",
    }, path=path)
    append_event({
        "kind": "sms_received",
        "handle": "crew-message",
        "sender_address": "+15551111111",
        "contact_name": "Beau",
        "body": "crew secret",
        "seen_at": "2026-08-12T10:00:00+00:00",
    }, path=path)
    append_event({
        "kind": "ancs_notification",
        "notification_id": 2,
        "app_id": "com.apple.MobileSMS",
        "title": "Beau",
        "subtitle": "Crew",
        "body": "crew secret",
        "seen_at": "2026-08-12T10:00:02+00:00",
    }, path=path)
    operations = _operations()

    assert operations.list_threads(10)[0]["key"] == key
    assert operations.delete_threads([key], True) == 1

    assert read_events(path=path) == []
    assert operations.list_threads(10) == []


def test_delete_threads_validates_entire_request_before_erasing(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "events.sqlite"
    monkeypatch.setattr(config, "EVENTS_DB", path)
    append_event({
        "kind": "sms_received",
        "sender_address": "+15551111111",
        "contact_name": "Alice",
        "body": "keep until valid request",
        "seen_at": "2026-08-12T10:00:00+00:00",
    }, path=path)
    operations = _operations()
    key = operations.list_threads(10)[0]["key"]

    with pytest.raises(ConfirmationRequiredError):
        operations.delete_threads([key], False)
    with pytest.raises(NotFoundError):
        operations.delete_threads([key, "address:phone:missing"], True)

    assert len(read_events(path=path)) == 1
