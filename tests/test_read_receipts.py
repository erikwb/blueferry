"""Phone reads wait for ANCS without delaying local reads or blocking OBEX."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import config, read_receipts
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.history import append_event
from blueferry.read_receipts import READ_RECEIPT_DELAY_SECONDS, ReadReceiptQueue
from blueferry.sinks.libnotify import LibnotifySink


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.timers = {}
        self.next_id = 0

    def schedule(self, delay, callback):
        self.next_id += 1
        self.timers[self.next_id] = (self.now + delay, callback)
        return self.next_id

    def cancel(self, timer):
        del self.timers[timer]

    def advance(self, seconds):
        target = self.now + seconds
        while self.timers:
            timer = min(self.timers, key=lambda key: self.timers[key][0])
            deadline, callback = self.timers[timer]
            if deadline > target:
                break
            del self.timers[timer]
            self.now = deadline
            assert callback() is False
        self.now = target


@pytest.fixture
def receipts(monkeypatch):
    clock = _Clock()
    jobs = []
    writes = []
    sessions = SimpleNamespace(map=object(), map_path="/session/map")
    monkeypatch.setattr(
        read_receipts, "set_session_messages_read",
        lambda path, handles: writes.append((path, list(handles))),
    )
    queue = ReadReceiptQueue(
        sessions,
        submit=lambda operation, **_callbacks: jobs.append(operation),
        schedule=clock.schedule,
        cancel=clock.cancel,
        clock=lambda: clock.now,
    )
    return SimpleNamespace(queue=queue, clock=clock, sessions=sessions, jobs=jobs, writes=writes)


def test_reads_wait_without_occupying_obex_and_complete_without_ancs(receipts):
    r = receipts
    r.queue.defer("/session/map", ["message-1"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 0.1)
    assert r.jobs == r.writes == []
    r.clock.advance(0.1)
    assert len(r.jobs) == 1
    assert r.writes == []
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["message-1"])]
    assert r.clock.timers == {}


def test_duplicate_reads_do_not_extend_delay_and_new_reads_get_the_full_delay(receipts):
    r = receipts
    r.queue.defer("/session/map", ["message-1"])
    r.clock.advance(2)
    r.queue.defer_path("/session/map/message-1")  # The same popup was also dismissed.
    r.queue.defer("/session/map", ["message-2"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 2)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["message-1"])]
    r.clock.advance(2)
    r.jobs.pop()()
    assert r.writes[-1] == ("/session/map", ["message-2"])
    assert len(r.writes) == 2


@pytest.mark.parametrize("worker_already_queued", [False, True])
@pytest.mark.parametrize("replacement", [None, "same-path", "different-path"])
def test_disconnect_or_reconnect_discards_stale_receipts(receipts, replacement, worker_already_queued):
    r = receipts
    r.queue.defer("/session/map", ["message-1"])
    if worker_already_queued:
        r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.sessions.map = None if replacement is None else object()
    if replacement == "different-path":
        r.sessions.map_path = "/session/replacement"
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    for job in r.jobs:
        job()
    assert r.writes == []
    assert r.clock.timers == {}


def test_new_session_reads_do_not_inherit_old_deadlines(receipts):
    r = receipts
    r.queue.defer("/session/map", ["old-message"])
    r.clock.advance(2)
    r.sessions.map = object()  # obexd can reuse object paths after restarting.
    r.queue.defer("/session/map", ["new-message"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 2)
    assert r.jobs == []
    r.clock.advance(2)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["new-message"])]


@pytest.mark.parametrize("worker_already_queued", [False, True])
def test_shutdown_cancels_pending_and_queued_receipts(receipts, worker_already_queued):
    r = receipts
    r.queue.defer("/session/map", ["message-1"])
    if worker_already_queued:
        r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.queue.close()
    r.queue.close()
    r.queue.defer("/session/map", ["message-2"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    for job in r.jobs:
        job()
    assert r.clock.timers == {}
    assert r.writes == []


def test_stale_popup_paths_are_ignored(receipts):
    r = receipts
    r.queue.defer_path("/old-session/message-1")
    assert r.clock.timers == {}
    assert r.jobs == []


def test_full_queue_never_flushes_early(receipts, monkeypatch):
    r = receipts
    monkeypatch.setattr(read_receipts, "MAX_PENDING_READ_RECEIPTS", 2)
    r.queue.defer("/session/map", ["one", "two", "three"])
    assert r.jobs == []
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["one", "two"])]


def test_worker_rejection_does_not_break_later_reads(receipts):
    r = receipts
    submit = r.queue._submit

    def full(*_args, **_kwargs):
        raise RuntimeError("OBEX operation queue is full")

    r.queue._submit = full
    r.queue.defer("/session/map", ["one"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.queue._submit = submit
    r.queue.defer("/session/map", ["two"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["two"])]


def test_late_ancs_groups_a_message_already_read_locally(receipts, monkeypatch, tmp_path):
    r = receipts
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    append_event({
        "kind": "sms_received", "handle": "message-1", "sender_address": "+15551111111",
        "contact_name": "Alice", "body": "hello team", "is_read": False,
        "seen_at": "2026-09-15T12:00:00Z",
    })
    operations = BackendOperations(
        r.sessions, BackendDependencies(defer_mark_read=r.queue.defer),
    )
    thread = operations.list_threads(10)[0]
    assert not thread["is_group"]
    assert operations.mark_thread_read(thread["key"]) == 1
    assert operations.list_threads(10)[0]["unread"] is False
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 1)
    assert r.jobs == []

    append_event({
        "kind": "ancs_notification", "notification_id": 42, "app_id": "com.apple.MobileSMS",
        "title": "Alice", "subtitle": "Team", "body": "hello team",
        "seen_at": "2026-09-15T12:00:04Z",
    })
    thread = operations.list_threads(10)[0]
    assert thread["is_group"] and thread["name"] == "Team"
    assert thread["unread"] is False
    assert thread["messages"][0]["handle"] == "message-1"
    r.clock.advance(1)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["message-1"])]


def test_popup_dismissal_uses_the_same_grace_period(receipts):
    r = receipts
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._pending = {7: "/session/map/message-1"}
    sink._msg_subs = {}
    sink._defer_mark_read = r.queue.defer_path
    sink._on_closed(7, 2)
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 1)
    assert r.jobs == []
    r.clock.advance(1)
    r.jobs.pop()()
    assert r.writes == [("/session/map", ["message-1"])]
