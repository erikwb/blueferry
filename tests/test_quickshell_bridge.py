from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

from blueferry.quickshell_bridge import QuickshellBridge, _RequestWorkers


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self):
        return SimpleNamespace(to_dict=lambda: {"daemon": True})

    def find_contacts(self, query):
        self.calls.append(("contacts", query))
        return [("Alice", "+15551234567")]

    def send(self, recipient, body):
        self.calls.append(("send", recipient, body))
        return "message-handle"

    def send_to_thread(self, thread_key, body, *, confirm_group):
        self.calls.append(("send_to_thread", thread_key, body, confirm_group))
        return "thread-message-handle"

    def set_group_participants(self, thread_key, recipients):
        self.calls.append(("set_group_participants", thread_key, recipients))
        return SimpleNamespace(to_dict=lambda: {"key": thread_key})

    def delete_threads(self, thread_keys):
        self.calls.append(("delete_threads", thread_keys))
        return len(thread_keys)

    def set_contacts_only_notifications(self, enabled):
        self.calls.append(("set_contacts_only_notifications", enabled))
        return enabled


def test_bridge_dispatches_private_values_without_command_arguments() -> None:
    client = FakeClient()
    bridge = QuickshellBridge(client)  # type: ignore[arg-type]

    assert bridge.dispatch("contacts", {"query": "private search"}) == [
        {"name": "Alice", "address": "+15551234567"}
    ]
    assert bridge.dispatch("send", {
        "recipient": "+15557654321", "body": "private body"
    }) == "message-handle"
    assert bridge.dispatch("send_to_thread", {
        "thread_key": "private-thread",
        "body": "group secret",
        "confirm_group": True,
    }) == "thread-message-handle"
    bridge.dispatch("set_group_participants", {
        "thread_key": "private-thread",
        "recipients": ["+15550000001", "+15550000002"],
    })
    assert bridge.dispatch("delete_threads", {
        "thread_keys": ["thread-one", "thread-two"],
    }) == 2
    assert bridge.dispatch("set_contacts_only_notifications", {
        "enabled": True,
    }) is True

    assert client.calls == [
        ("contacts", "private search"),
        ("send", "+15557654321", "private body"),
        ("send_to_thread", "private-thread", "group secret", True),
        (
            "set_group_participants",
            "private-thread",
            ["+15550000001", "+15550000002"],
        ),
        ("delete_threads", ["thread-one", "thread-two"]),
        ("set_contacts_only_notifications", True),
    ]


def test_bridge_rejects_non_boolean_contacts_only_value() -> None:
    bridge = QuickshellBridge(FakeClient())  # type: ignore[arg-type]

    try:
        bridge.dispatch("set_contacts_only_notifications", {"enabled": 1})
    except ValueError as error:
        assert str(error) == "enabled must be a boolean"
    else:
        raise AssertionError("non-boolean preference was accepted")


def test_bridge_returns_structured_success_and_errors() -> None:
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output)  # type: ignore[arg-type]

    bridge.handle_line('{"id":7,"method":"status","args":{}}')
    bridge.handle_line('{"id":8,"method":"unknown","args":{}}')

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == {
        "id": 7,
        "method": "status",
        "ok": True,
        "result": {"daemon": True},
    }
    assert replies[1]["id"] == 8
    assert replies[1]["method"] == "unknown"
    assert replies[1]["ok"] is False
    assert replies[1]["error"] == "unsupported method: unknown"


def test_slow_send_does_not_block_status_requests() -> None:
    send_started = threading.Event()
    release_send = threading.Event()
    status_called = threading.Event()

    class BlockingClient(FakeClient):
        def send(self, recipient, body):
            send_started.set()
            assert release_send.wait(2)
            return "message-handle"

        def status(self):
            status_called.set()
            return super().status()

    bridge = QuickshellBridge(BlockingClient(), io.StringIO())  # type: ignore[arg-type]
    workers = _RequestWorkers(bridge)
    workers.submit('{"id":1,"method":"send","args":{"recipient":"a","body":"b"}}')
    assert send_started.wait(1)

    workers.submit('{"id":2,"method":"status","args":{}}')
    assert status_called.wait(1)
    release_send.set()
