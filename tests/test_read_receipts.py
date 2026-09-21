"""Phone reads wait for ANCS without delaying local reads or blocking OBEX."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import config, read_receipts
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.history import append_event
from blueferry.obex import map_read
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
        map_read, "set_message_read", writes.append,
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
    assert r.writes == ["/session/map/message-1"]
    assert r.clock.timers == {}


def test_duplicate_reads_do_not_extend_delay_and_new_reads_get_the_full_delay(receipts):
    r = receipts
    r.queue.defer("/session/map", ["message-1"])
    r.clock.advance(2)
    r.queue.defer_path("/session/map/message-1")  # The same popup was also dismissed.
    r.queue.defer("/session/map", ["message-2"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 2)
    r.jobs.pop()()
    assert r.writes == ["/session/map/message-1"]
    r.clock.advance(2)
    r.jobs.pop()()
    assert r.writes[-1] == "/session/map/message-2"
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
    assert r.writes == ["/session/map/new-message"]


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


@pytest.mark.parametrize("interruption", ["shutdown", "disconnect", "same-path", "different-path"])
def test_interruption_during_a_write_stops_the_rest_of_the_batch(receipts, monkeypatch, interruption):
    r = receipts

    def write(path):
        r.writes.append(path)
        if interruption == "shutdown":
            r.queue.close()
        elif interruption == "disconnect":
            r.sessions.map = None
        else:
            r.sessions.map = object()
            if interruption == "different-path":
                r.sessions.map_path = "/session/replacement"

    monkeypatch.setattr(map_read, "set_message_read", write)
    r.queue.defer("/session/map", ["one", "two", "three"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.jobs.pop()()
    assert r.writes == ["/session/map/one"]


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
    assert r.writes == ["/session/map/one", "/session/map/two"]


def test_worker_rejection_retries_the_original_read_without_another_request(receipts):
    r = receipts
    submit = r.queue._submit

    def full(*_args, **_kwargs):
        raise RuntimeError("OBEX operation queue is full")

    r.queue._submit = full
    r.queue.defer("/session/map", ["one"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.queue._submit = submit
    r.clock.advance(1)
    assert len(r.jobs) == 1
    r.jobs.pop()()
    assert r.writes == ["/session/map/one"]
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    assert r.jobs == []
    assert r.clock.timers == {}


def test_repeated_rejections_preserve_deadlines_and_do_not_acknowledge_new_reads_early(receipts):
    r = receipts
    attempts = []

    def submit(operation, **_kwargs):
        attempts.append(r.clock.now)
        if len(attempts) <= 3:
            raise RuntimeError("OBEX operation queue is full")
        r.jobs.append(operation)

    r.queue._submit = submit
    r.queue.defer("/session/map", ["one"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    r.queue.defer("/session/map", ["one", "two"])
    r.clock.advance(3)
    assert attempts == [105, 106, 107, 108]
    assert len(r.jobs) == 1
    r.jobs.pop()()
    assert r.writes == ["/session/map/one"]
    r.clock.advance(2)
    r.jobs.pop()()
    assert r.writes == ["/session/map/one", "/session/map/two"]
    assert r.clock.timers == {}


@pytest.mark.parametrize("interruption", ["shutdown", "disconnect", "reconnect"])
def test_retry_is_discarded_on_shutdown_or_session_loss(receipts, interruption):
    r = receipts
    submit = r.queue._submit

    def full(*_args, **_kwargs):
        raise RuntimeError("OBEX operation queue is full")

    r.queue._submit = full
    r.queue.defer("/session/map", ["one"])
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    assert r.clock.timers
    if interruption == "shutdown":
        r.queue.close()
    else:
        r.sessions.map = None if interruption == "disconnect" else object()
    r.queue._submit = submit
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS)
    assert r.jobs == []
    assert r.clock.timers == {}


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
    assert r.writes == ["/session/map/message-1"]


def test_popup_dismissal_uses_the_same_grace_period(receipts, monkeypatch):
    r = receipts
    monkeypatch.setattr(config, "MARK_READ_ON_DISMISS", True)
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._pending = {7: "/session/map/message-1"}
    sink._msg_subs = {}
    sink._defer_mark_read = r.queue.defer_path
    sink._on_closed(7, 2)
    r.clock.advance(READ_RECEIPT_DELAY_SECONDS - 1)
    assert r.jobs == []
    r.clock.advance(1)
    r.jobs.pop()()
    assert r.writes == ["/session/map/message-1"]
