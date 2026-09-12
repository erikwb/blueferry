from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from blueferry.models import Thread
from blueferry.quickshell_bridge import QuickshellBridge, RequestError, _RequestWorkers


def test_only_the_desktop_bridge_can_change_the_preferred_client(monkeypatch):
    active = []
    monkeypatch.setattr("blueferry.quickshell_bridge.record_client_use", active.append)
    widget = QuickshellBridge(FakeClient())
    with pytest.raises(RequestError, match="unsupported method"):
        widget.dispatch("client_active", {})
    assert active == []
    desktop = QuickshellBridge(FakeClient(), desktop_client=True)
    desktop.dispatch("client_active", {})
    assert active == ["quickshell"]


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

    def send_to_thread(self, thread_key, body, *, confirm_group, expected_group_token):
        self.calls.append(("send_to_thread", thread_key, body, confirm_group, expected_group_token))
        return "thread-message-handle"

    def set_group_participants(self, thread_key, recipients):
        self.calls.append(("set_group_participants", thread_key, recipients))
        return SimpleNamespace(to_dict=lambda: {"key": thread_key})

    def delete_threads(self, thread_keys):
        self.calls.append(("delete_threads", thread_keys))
        return len(thread_keys)

    def mark_thread_read(self, thread_key):
        self.calls.append(("mark_thread_read", thread_key))
        return 2

    def set_thread_starred(self, thread_key, starred):
        self.calls.append(("set_thread_starred", thread_key, starred))
        return starred

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
        "expected_group_token": "roster-token",
    }) == "thread-message-handle"
    bridge.dispatch("set_group_participants", {
        "thread_key": "private-thread",
        "recipients": ["+15550000001", "+15550000002"],
    })
    assert bridge.dispatch("delete_threads", {
        "thread_keys": ["thread-one", "thread-two"],
    }) == 2
    assert bridge.dispatch("mark_thread_read", {
        "thread_key": "private-thread",
    }) == 2
    assert bridge.dispatch("set_thread_starred", {
        "thread_key": "private-thread",
        "starred": True,
    }) is True
    assert bridge.dispatch("set_contacts_only_notifications", {
        "enabled": True,
    }) is True

    assert client.calls == [
        ("contacts", "private search"),
        ("send", "+15557654321", "private body"),
        ("send_to_thread", "private-thread", "group secret", True, "roster-token"),
        (
            "set_group_participants",
            "private-thread",
            ["+15550000001", "+15550000002"],
        ),
        ("delete_threads", ["thread-one", "thread-two"]),
        ("mark_thread_read", "private-thread"),
        ("set_thread_starred", "private-thread", True),
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


def test_bridge_supplies_shared_thread_metadata_and_forwards_displayed_approval():
    class Client(FakeClient):
        def threads(self, limit=200):
            return [Thread.from_dict({
                "key": "group:crew", "is_group": True, "roster_warning_id": "warning:1",
                "recipients": ["Bob@example.com", "+1 (555) 222-2222"],
                "messages": [{"outgoing": False, "read": False}],
            })]

    client = Client()
    bridge = QuickshellBridge(client)
    payload = bridge.dispatch("threads", {})[0]
    assert payload["confirmation_token"] == "warning:1\n+1 (555) 222-2222\nBob@example.com"
    assert payload["roster_warning_key"] == "warning:1"
    assert payload["unread"] is True
    assert payload["unread_count"] == 1
    bridge.dispatch("send_to_thread", {
        "thread_key": payload["key"], "body": "draft", "confirm_group": True,
        "expected_group_token": payload["confirmation_token"],
    })
    assert client.calls == [(
        "send_to_thread", "group:crew", "draft", True, payload["confirmation_token"],
    )]


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


def test_focus_bypasses_busy_backend_workers_and_preserves_newer_client(monkeypatch, tmp_path):
    from blueferry import client_activation
    from blueferry.quickshell_bridge import REQUEST_WORKERS

    monkeypatch.setattr(client_activation.config, "CONFIG_DIR", tmp_path)
    busy = threading.Barrier(REQUEST_WORKERS + 1)
    release = threading.Event()

    class BlockingClient(FakeClient):
        def status(self):
            busy.wait(timeout=5)
            assert release.wait(5)
            return super().status()

    output = io.StringIO()
    bridge = QuickshellBridge(BlockingClient(), output, desktop_client=True)
    workers = _RequestWorkers(bridge)
    try:
        for index in range(REQUEST_WORKERS):
            workers.submit(json.dumps({"id": index, "method": "status", "args": {}}))
        busy.wait(timeout=5)
        workers.submit('{"id":100,"method":"client_active","args":{}}')
        assert json.loads(output.getvalue()) == {
            "id": 100, "method": "client_active", "ok": True, "result": None,
        }
        client_activation.record_client_use("gtk")
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while len(output.getvalue().splitlines()) < REQUEST_WORKERS + 1:
        assert time.monotonic() < deadline
        time.sleep(.01)
    running = [client.bus_name for client in client_activation.CLIENTS]
    assert client_activation.select_client(running).key == "gtk"


@pytest.mark.parametrize("line,desktop", [
    ('{"id":true,"method":"client_active","args":{}}', True),
    ('{"id":1,"method":"client_active","args":[]}', True),
    ('{"id":1,"method":"client_active","args":{}}', False),
])
def test_immediate_focus_still_validates_requests(monkeypatch, line, desktop):
    recorded = []
    monkeypatch.setattr("blueferry.quickshell_bridge.record_client_use", recorded.append)
    output = io.StringIO()
    bridge = QuickshellBridge(FakeClient(), output, desktop_client=desktop)
    workers = _RequestWorkers(bridge)
    workers.submit(line)
    assert not json.loads(output.getvalue())["ok"]
    assert not recorded


def test_stdin_reader_drains_batched_lines_without_another_write():
    import os

    from blueferry.quickshell_bridge import _read_requests

    read_fd, write_fd = os.pipe()
    received = []
    completed = threading.Event()

    def submit(line):
        received.append(line)
        if len(received) == 3:
            completed.set()

    with os.fdopen(read_fd) as stream:
        reader = threading.Thread(target=_read_requests, args=(stream, submit), daemon=True)
        reader.start()
        try:
            os.write(write_fd, b"one\ntwo\nthree\n")
            assert completed.wait(2), received
            assert received == ["one\n", "two\n", "three\n"]
        finally:
            os.close(write_fd)
            reader.join(2)
        assert not reader.is_alive()


def test_stdin_reader_discards_oversized_line_and_recovers(monkeypatch):
    from blueferry import quickshell_bridge

    monkeypatch.setattr(quickshell_bridge, "MAX_REQUEST_CHARS", 8)
    received = []
    quickshell_bridge._read_requests(io.StringIO("x" * 40 + "\nnext\n"), received.append)
    assert len(received) == 2
    assert len(received[0]) > 8
    assert received[1] == "next\n"
