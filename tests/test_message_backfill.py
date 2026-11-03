"""Missed-message backfill after a MAP reconnect, without a phone or bus."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from blueferry import history
from blueferry import message_backfill as backfill
from blueferry.events import SmsEvent
from blueferry.obex import map_query
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity

NOW = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
SESSION = "/org/bluez/obex/client/session3"
ALICE = "+15551230001"
BOB = "+15551230002"


def _entry(handle, *, sender=ALICE, body="hello", minutes_ago=10, read=False, **extra):
    stamp = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y%m%dT%H%M%SZ")
    entry = {
        "handle": handle, "sender_address": sender, "sender_name": "",
        "body": body, "timestamp": stamp, "read": read, "type": "sms-gsm",
    }
    entry.update(extra)
    return entry


def _listed(*entries):
    return backfill.parse_listing(entries, now=NOW)


def _stored(handle, *, sender=ALICE, body="hello", minutes_ago=10, timestamp=True,
            is_read=False):
    when = NOW - timedelta(minutes=minutes_ago)
    return {
        "kind": "sms_received", "handle": handle, "sender_address": sender,
        "body": body, "is_read": is_read,
        "timestamp": when.isoformat() if timestamp else None,
        "seen_at": when.isoformat(),
    }


# -- listing validation -----------------------------------------------------

def test_listing_keeps_only_entries_that_can_be_placed_safely():
    listed = _listed(
        _entry("message1"),
        _entry("message2", type="email"),
        _entry("message3", timestamp=""),
        _entry("message4", timestamp="garbage"),
        _entry("message5", sender="Mom"),
        _entry("message6", body="   "),
        _entry("message7", minutes_ago=-3 * 24 * 60),
        _entry("", body="no handle"),
    )
    assert [message.handle for message in listed] == ["message1"]


def test_listing_keeps_keypad_spelled_senders_out_of_reply_routing():
    [message] = _listed(_entry("message1", sender="6262", sender_name="MBNA"))
    assert message.sender_alphanumeric == "MBNA"
    payload = backfill.history_payload(message, contact_name="Someone")
    assert payload["sender_alphanumeric"] == "MBNA"
    assert payload["contact_name"] is None


@pytest.mark.parametrize(("flag", "expected"), [(True, True), (False, False), (None, False)])
def test_listing_takes_the_read_flag_from_the_phone(flag, expected):
    [message] = _listed(_entry("message1", read=flag))
    assert message.read is expected


# -- what to list -----------------------------------------------------------

def test_empty_history_lists_the_retention_window():
    assert backfill.backfill_since([], now=NOW, retention_days=30) == NOW - timedelta(days=30)


def test_listing_starts_a_margin_before_the_newest_received_message():
    events = [
        _stored("message1", minutes_ago=600),
        _stored("message2", minutes_ago=60),
        {"kind": "sms_sent", "timestamp": NOW.isoformat()},
    ]
    since = backfill.backfill_since(events, now=NOW, retention_days=30)
    assert since == NOW - timedelta(minutes=60) - backfill.SAFETY_MARGIN


def test_retention_and_erase_floor_bound_the_listing():
    old = [_stored("message1", minutes_ago=40 * 24 * 60)]
    assert backfill.backfill_since(old, now=NOW, retention_days=7) == NOW - timedelta(days=7)
    floor = NOW - timedelta(minutes=5)
    since = backfill.backfill_since(
        [_stored("message2", minutes_ago=60)], now=NOW, retention_days=30, floor=floor,
    )
    assert since == floor


def test_a_bogus_future_timestamp_does_not_stop_later_backfills():
    events = [_stored("message1", minutes_ago=-10 * 24 * 60), _stored("message2", minutes_ago=30)]
    since = backfill.backfill_since(events, now=NOW, retention_days=30)
    assert since == NOW - timedelta(minutes=30) - backfill.SAFETY_MARGIN


# -- deduplication ----------------------------------------------------------

def test_messages_already_in_history_are_not_added_again():
    listed = _listed(
        _entry("message1", body="seen live"),
        _entry("message2", body="missed", minutes_ago=5),
    )
    # A live MNS event often has no phone timestamp; its arrival time counts.
    stored = [_stored("message9", body="seen live", minutes_ago=9, timestamp=False)]
    plan = backfill.plan_backfill(listed, stored, since=NOW - timedelta(hours=3))
    assert [message.body for message in plan.missing] == ["missed"]


def test_same_handle_matches_even_far_apart_in_time():
    listed = _listed(_entry("message1", minutes_ago=10))
    stored = [_stored("message1", minutes_ago=10 + 24 * 60)]
    plan = backfill.plan_backfill(listed, stored, since=NOW - timedelta(days=3))
    assert plan.missing == ()


def test_a_repeated_text_is_matched_one_to_one():
    listed = _listed(
        _entry("message1", body="ok", minutes_ago=50),
        _entry("message2", body="ok", minutes_ago=20),
    )
    stored = [_stored("message7", body="ok", minutes_ago=49)]
    plan = backfill.plan_backfill(listed, stored, since=NOW - timedelta(hours=3))
    assert [message.handle for message in plan.missing] == ["message2"]


def test_other_senders_and_texts_are_not_duplicates():
    listed = _listed(_entry("message1", sender=BOB), _entry("message2", body="other"))
    stored = [_stored("message9")]
    plan = backfill.plan_backfill(listed, stored, since=NOW - timedelta(hours=3))
    assert {message.handle for message in plan.missing} == {"message1", "message2"}


def test_a_shortened_listing_subject_matches_the_full_stored_body():
    body = "x" * 40 + " and the rest of a long message"
    listed = _listed(_entry("message1", body=body[:40] + "…"))
    plan = backfill.plan_backfill(listed, [_stored("message5", body=body)],
                                  since=NOW - timedelta(hours=3))
    assert plan.missing == ()


def test_only_messages_inside_the_window_and_limit_are_added():
    listed = _listed(*[
        _entry(f"message{index}", body=f"text {index}", minutes_ago=index)
        for index in range(1, 8)
    ])
    plan = backfill.plan_backfill(listed, [], since=NOW - timedelta(minutes=5), limit=3)
    # Newest three of the five inside the window, oldest first.
    assert [message.handle for message in plan.missing] == ["message3", "message2", "message1"]


def test_stored_messages_read_on_the_phone_are_marked_read():
    listed = _listed(_entry("message1", read=True), _entry("message2", body="b", read=False))
    stored = [_stored("old1"), _stored("old2", body="b")]
    plan = backfill.plan_backfill(listed, stored, since=NOW - timedelta(hours=3))
    assert plan.read_handles == ("old1",)


def test_payload_is_history_with_the_phone_time_and_no_group_guess():
    [message] = _listed(_entry("message1", read=True, minutes_ago=42))
    payload = backfill.history_payload(message, contact_name="Alice")
    stamp = (NOW - timedelta(minutes=42)).isoformat()
    assert payload["kind"] == "sms_received"
    assert payload["timestamp"] == stamp
    assert payload["seen_at"] == stamp
    assert payload["is_read"] is True
    assert payload["contact_name"] == "Alice"
    assert payload["group_ambiguous"] is True
    assert payload["backfilled"] is True
    assert payload["group_key"] is None


# -- coordinator ------------------------------------------------------------

class Worker:
    def __init__(self):
        self.jobs = []
        self.refuse = False

    def submit(self, operation, *, on_success=None, on_error=None):
        if self.refuse:
            raise RuntimeError("Bluetooth recovery is in progress")
        self.jobs.append((operation, on_success, on_error))

    def run_next(self):
        operation, on_success, on_error = self.jobs.pop(0)
        try:
            result = operation()
        except Exception as error:
            on_error(error)
        else:
            on_success(result)


class Harness:
    def __init__(self, listing=(), stored=(), enabled=True):
        self.worker = Worker()
        self.timers = {}
        self.listing = list(listing)
        self.stored = list(stored)
        self.enabled = enabled
        self.session = SESSION
        self.writes = []
        self.floors = []
        self.floor = None
        self.changed = 0
        self.calls = []
        self.list_error = None
        self.coordinator = backfill.MessageBackfill(
            enabled=lambda: self.enabled,
            session_path=lambda: self.session,
            submit=self.worker.submit,
            list_inbox=self.list_inbox,
            collect=self.collect,
            store=self.store,
            resolve_contact=lambda raw: "Alice" if raw == ALICE else None,
            on_changed=self.on_changed,
            schedule=self.schedule,
            cancel=self.timers.pop,
            now=lambda: NOW,
            retention_days=lambda: 30,
        )

    def schedule(self, delay, callback):
        source = len(self.timers) + 100
        self.timers[source] = (delay, callback)
        return source

    def fire(self):
        [source] = list(self.timers)
        _delay, callback = self.timers.pop(source)
        assert callback() is False

    def list_inbox(self, session, since, limit):
        self.calls.append(("list", session, since, limit))
        if self.list_error is not None:
            raise self.list_error
        return list(self.listing)

    def collect(self):
        self.calls.append("collect")
        return list(self.stored), self.floor

    def store(self, payloads, handles, floor):
        self.writes.append((payloads, handles))
        self.floors.append(floor)
        return True

    def on_changed(self):
        self.changed += 1


def test_backfill_does_nothing_while_message_history_is_off():
    harness = Harness(enabled=False)
    harness.coordinator.request("MAP connected")
    assert harness.timers == {}
    assert harness.worker.jobs == []


def test_backfill_needs_a_map_session():
    harness = Harness()
    harness.session = None
    harness.coordinator.request("storage changed")
    assert harness.timers == {}


def test_listing_and_history_scan_run_only_on_the_worker():
    harness = Harness(listing=[_entry("message1", body="missed")])
    harness.coordinator.request("MAP connected")
    harness.coordinator.request("MAP notifications connected")
    assert [delay for delay, _ in harness.timers.values()] == [backfill.BACKFILL_DELAY_SECONDS]
    harness.fire()
    # Firing the timer queued work; nothing touched the phone or the store yet.
    assert harness.calls == []
    assert len(harness.worker.jobs) == 1
    harness.worker.run_next()
    assert harness.calls[0] == "collect"
    assert harness.calls[1][:2] == ("list", SESSION)
    assert harness.calls[1][3] == backfill.MAX_BACKFILL_MESSAGES
    assert harness.writes == []  # The write is a second worker job.
    harness.worker.run_next()
    [(payloads, handles)] = harness.writes
    assert [payload["body"] for payload in payloads] == ["missed"]
    assert handles == []
    assert harness.changed == 1


def test_a_live_copy_during_the_listing_is_stored_once():
    harness = Harness(listing=[_entry("message1", body="racing", minutes_ago=0)])
    harness.coordinator.request("MAP connected")
    harness.fire()
    live = SmsEvent(
        kind="sms_received", handle="message1", sender_address=ALICE,
        sender_phone_norm="15551230001", contact_name="Alice", body="racing",
        timestamp=None, is_read=False, seen_at=NOW,
    )
    assert harness.coordinator.live_message(live) is True
    harness.worker.run_next()
    assert harness.worker.jobs == []
    assert harness.writes == []


def test_a_live_copy_after_the_backfill_notifies_without_a_second_row():
    harness = Harness(listing=[_entry("message1", body="late", minutes_ago=0)])
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.worker.run_next()
    live = SmsEvent(
        kind="sms_received", handle="message1", sender_address=ALICE,
        sender_phone_norm="15551230001", contact_name="Alice", body="late",
        timestamp=None, is_read=False, seen_at=NOW,
    )
    assert harness.coordinator.live_message(live) is False
    # A second identical text is a new message.
    assert harness.coordinator.live_message(live) is True


def test_a_new_live_message_with_the_same_text_is_still_stored():
    harness = Harness(listing=[_entry("message1", body="ok", minutes_ago=20)])
    harness.coordinator.request("MAP connected")
    harness.fire()
    live = SmsEvent(
        kind="sms_received", handle="message2", sender_address=ALICE,
        sender_phone_norm=None, contact_name=None, body="ok",
        timestamp=None, is_read=False, seen_at=NOW,
    )
    # During the listing: the missed "ok" is still backfilled.
    assert harness.coordinator.live_message(live) is True
    harness.worker.run_next()
    harness.worker.run_next()
    [(payloads, _handles)] = harness.writes
    assert [payload["handle"] for payload in payloads] == ["message1"]
    # After it: another "ok" is a new message, not the backfilled one.
    assert harness.coordinator.live_message(live) is True


def test_listing_failure_is_logged_without_content_and_retried_next_time(caplog):
    harness = Harness()
    harness.list_error = RuntimeError(f"{ALICE} secret text")
    harness.coordinator.request("MAP connected")
    harness.fire()
    with caplog.at_level(logging.DEBUG, logger="blueferry.message_backfill"):
        harness.worker.run_next()
    assert "RuntimeError" in caplog.text
    assert ALICE not in caplog.text and "secret" not in caplog.text
    assert harness.writes == []
    harness.coordinator.request("MAP notifications connected")
    assert len(harness.timers) == 1


def test_a_refused_submission_leaves_the_backfill_ready_for_the_next_connect():
    harness = Harness()
    harness.worker.refuse = True
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.worker.refuse = False
    harness.coordinator.request("MAP notifications connected")
    assert len(harness.timers) == 1


def test_a_store_failure_forgets_the_messages_it_did_not_write():
    harness = Harness(listing=[_entry("message1", body="lost", minutes_ago=0)])

    def broken(_payloads, _handles):
        raise OSError("disk full")

    harness.coordinator._store = broken
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.worker.run_next()
    harness.worker.run_next()
    live = SmsEvent(
        kind="sms_received", handle="message1", sender_address=ALICE,
        sender_phone_norm=None, contact_name=None, body="lost",
        timestamp=None, is_read=False, seen_at=NOW,
    )
    assert harness.coordinator.live_message(live) is True
    assert harness.changed == 0


def test_results_for_a_lost_or_replaced_session_are_dropped():
    harness = Harness(listing=[_entry("message1", body="stale")])
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.session = "/org/bluez/obex/client/session4"
    harness.worker.run_next()
    assert harness.worker.jobs == []

    harness.session = SESSION
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.coordinator.stop()
    harness.worker.run_next()
    assert harness.worker.jobs == []
    assert harness.writes == []


def test_stop_cancels_a_pending_timer():
    harness = Harness()
    harness.coordinator.request("MAP connected")
    harness.coordinator.stop()
    assert harness.timers == {}


def test_a_reconnect_during_a_backfill_lists_again_afterwards():
    harness = Harness()
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.coordinator.request("MAP notifications connected")
    assert harness.timers == {}
    harness.worker.run_next()
    assert len(harness.timers) == 1


def test_logs_carry_counts_and_duration_only(caplog):
    harness = Harness(listing=[_entry("message1", body="very private words")])
    harness.coordinator.request("MAP connected")
    harness.fire()
    with caplog.at_level(logging.DEBUG, logger="blueferry.message_backfill"):
        harness.worker.run_next()
        harness.worker.run_next()
    assert "added 1" in caplog.text
    assert "private" not in caplog.text
    assert ALICE not in caplog.text and "5551230001" not in caplog.text


# -- storage ----------------------------------------------------------------

class _Keys:
    def get_or_create(self, *, allow_prompt, cancellable=None, create=True):
        return b"K" * 32

    def delete(self, *, allow_prompt, cancellable=None):
        return True


@pytest.fixture
def storage(isolated_state):
    value = StorageSecurity(
        settings=SettingsStore(isolated_state / "config" / "settings.json"),
        key_provider=_Keys(),
    )
    yield value
    value.close()


def test_storage_round_trip_keeps_backfilled_messages_encrypted(storage):
    history.append_event(_stored("old1"), storage=storage)
    stored, floor = backfill.collect_history(storage)
    assert [event["handle"] for event in stored] == ["old1"]
    assert floor is None
    [message] = _listed(_entry("message1", body="missed"))
    payload = backfill.history_payload(message, contact_name=None)

    assert backfill.store_backfill(storage, [payload], ["old1"], floor) is True

    events = history.read_events(kinds={"sms_received"}, storage=storage)
    assert [event["handle"] for event in events] == ["old1", "message1"]
    assert events[0]["is_read"] is True
    raw = history.read_events()  # Without the key: ciphertext only.
    assert all("missed" not in str(event) for event in raw)


def test_storage_helpers_refuse_when_history_is_not_retained(storage):
    storage.set_policy("none", allow_prompt=False)
    assert not backfill.history_enabled(storage)
    with pytest.raises(RuntimeError):
        backfill.collect_history(storage)
    with pytest.raises(RuntimeError):
        backfill.store_backfill(storage, [{"kind": "sms_received"}], [], None)


def test_erasing_history_raises_the_backfill_floor(isolated_state):
    assert history.backfill_floor() is None
    history.append_event(_stored("old1"))
    before = datetime.now(timezone.utc).replace(microsecond=0)
    history.clear_events()
    floor = history.backfill_floor()
    assert floor is not None and floor >= before - timedelta(seconds=1)
    # The floor only moves forward.
    history.raise_backfill_floor(floor - timedelta(days=1))
    assert history.backfill_floor() == floor


@pytest.mark.parametrize("erase", ["clear", "delete"])
def test_an_erase_during_the_backfill_keeps_the_listed_messages_out(storage, erase):
    from blueferry.backend_operations import BackendDependencies, BackendOperations

    history.append_event(_stored("old1", body="erase me"), storage=storage)
    _stored_events, floor = backfill.collect_history(storage)
    [message] = _listed(_entry("message1", body="erase me too"))
    payload = backfill.history_payload(message, contact_name=None)
    # The user erases while the listing is in flight.
    if erase == "clear":
        history.clear_events()
    else:
        sessions = SimpleNamespace(map=None, pbap=None, map_path="")
        operations = BackendOperations(sessions, BackendDependencies(storage=storage))
        [thread] = operations.list_threads(10)
        assert operations.delete_threads([thread["key"]], True) == 1

    with pytest.raises(history.HistoryErasedError):
        backfill.store_backfill(storage, [payload], [], floor)
    assert history.read_events(storage=storage) == []


def test_the_store_gets_the_floor_the_plan_was_made_under(caplog):
    floor = NOW - timedelta(days=1)
    harness = Harness(listing=[_entry("message1", body="missed")])
    harness.floor = floor
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.worker.run_next()
    harness.worker.run_next()
    assert harness.floors == [floor]

    # An erase in between is not a failure, and the next connect lists again.
    def erased(*_args):
        raise history.HistoryErasedError("erased")

    harness.store = erased
    harness.coordinator._store = erased
    harness.coordinator.stop()
    harness.coordinator.request("MAP connected")
    harness.fire()
    harness.worker.run_next()
    with caplog.at_level(logging.INFO, logger=backfill.__name__):
        harness.worker.run_next()
    assert harness.changed == 1
    assert "erased meanwhile" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    harness.coordinator.request("MAP connected")
    assert len(harness.timers) == 1


def test_a_conversation_is_not_deleted_without_raising_the_floor(isolated_state, monkeypatch):
    import sqlite3

    from blueferry.backend_operations import BackendDependencies, BackendOperations
    from blueferry.errors import NotReadyError

    history.append_event(_stored("old1"))
    sessions = SimpleNamespace(map=None, pbap=None, map_path="")
    operations = BackendOperations(sessions, BackendDependencies())
    [thread] = operations.list_threads(10)

    def broken(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(history, "_raise_backfill_floor", broken)
    with pytest.raises(NotReadyError):
        operations.delete_threads([thread["key"]], True)
    # Erasing and raising the floor are one transaction: both or neither.
    assert [event["handle"] for event in history.read_events()] == ["old1"]


def test_deleting_a_conversation_raises_the_floor(isolated_state):
    from blueferry.backend_operations import BackendDependencies, BackendOperations

    history.append_event(_stored("old1"))
    sessions = SimpleNamespace(map=None, pbap=None, map_path="")
    operations = BackendOperations(sessions, BackendDependencies())
    [thread] = operations.list_threads(10)

    assert operations.delete_threads([thread["key"]], True) == 1

    assert history.backfill_floor() is not None


def _thread_operations():
    from blueferry.backend_operations import BackendDependencies, BackendOperations

    def submit(operation, *, on_success, on_error):
        try:
            on_success(operation())
        except Exception as error:
            on_error(error)

    sessions = SimpleNamespace(
        map=object(), pbap=None, map_path=SESSION, report_error=lambda _error: None,
    )
    return BackendOperations(sessions, BackendDependencies(submit_obex=submit))


def test_a_caught_up_message_offers_a_reply_to_the_sender_only(isolated_state, monkeypatch):
    from blueferry import backend_operations
    from blueferry.errors import NotReadyError

    [message] = _listed(_entry("message1", body="missed", minutes_ago=30))
    history.append_events([backfill.history_payload(message, contact_name="Alice")])
    operations = _thread_operations()
    [thread] = operations.list_threads(10)
    # Maybe from a group: not reply-ready, but the user may confirm a 1:1 reply.
    assert thread["group_ambiguous"] is True
    assert thread["reply_ready"] is False
    token = thread["sender_reply_token"]
    assert token
    sent = []
    monkeypatch.setattr(
        backend_operations, "send_message",
        lambda _path, recipient, body: sent.append((recipient, body)) or "/transfer/1",
    )
    with pytest.raises(NotReadyError):
        operations.send_to_thread(
            thread["key"], "draft", False,
            lambda _: pytest.fail("unexpected send"), lambda error: pytest.fail(str(error)),
        )
    replies = []
    operations.send_to_thread(
        thread["key"], "just you", True, replies.append,
        lambda error: pytest.fail(str(error)), expected_group_token=token,
    )
    assert replies == ["/transfer/1"]
    assert sent == [(ALICE, "just you")]

    # A live message from the sender makes the conversation reply-ready again.
    history.append_event(_stored("message2", body="live", minutes_ago=1))
    [thread] = operations.list_threads(10)
    assert thread["reply_ready"] is True
    assert not thread.get("sender_reply_token")


# -- MAP listing on the worker ------------------------------------------------

def test_inbox_listing_uses_bluez_filter_keys_and_listing_properties(monkeypatch):
    seen = {}

    class FakeMap:
        def SetFolder(self, folder, *, timeout):
            assert timeout <= 10

        def ListMessages(self, folder, filters, *, timeout):
            seen["filters"] = dict(filters)
            seen["signature"] = filters.signature
            return {
                SESSION + "/message1": {
                    "SenderAddress": ALICE, "Sender": "Alice", "Subject": "hi",
                    "Timestamp": "20261102T221200", "Read": False, "Type": "sms-gsm",
                },
                "/org/bluez/obex/client/session9/message2": {"Subject": "other"},
            }

    def obex(path, interface):
        assert (path, interface) == (SESSION, "org.bluez.obex.MessageAccess1")
        return FakeMap()

    monkeypatch.setattr(map_query, "obex", obex)
    since = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    [entry] = map_query.list_inbox_since(SESSION, since, 500)

    assert int(seen["filters"]["MaxCount"]) == map_query.MAX_RECENT_QUERY_LIMIT
    assert str(seen["filters"]["PeriodBegin"]) == since.astimezone().strftime("%Y%m%dT%H%M%S")
    assert seen["signature"] == "sv"
    assert entry == {
        "handle": "message1", "sender_address": ALICE, "sender_name": "Alice",
        "body": "hi", "timestamp": "20261102T221200", "read": False, "type": "sms-gsm",
    }


def test_inbox_listing_stops_at_its_deadline(monkeypatch):
    clock = [0.0]

    class FakeMap:
        def SetFolder(self, _folder, *, timeout):
            clock[0] += 30

        def ListMessages(self, *_args, **_kwargs):
            pytest.fail("an expired backfill must not list the inbox")

    monkeypatch.setattr(map_query, "obex", lambda *_args: FakeMap())
    monkeypatch.setattr(map_query.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        map_query.list_inbox_since(SESSION, None, 10)


# -- daemon wiring ------------------------------------------------------------

def test_daemon_live_copy_of_a_backfilled_message_skips_history(make_daemon):
    instance = make_daemon()
    delivered = []
    instance.events = SimpleNamespace(
        message=lambda event, persist=True: delivered.append(persist),
    )
    instance.backfill = SimpleNamespace(live_message=lambda event: False)
    instance._map_message(SimpleNamespace(kind="sms_received"))
    assert delivered == [False]


def test_daemon_connect_requests_backfill_only_with_history_on(make_daemon, monkeypatch):
    from blueferry import daemon as daemon_mod

    timers, removed = [], []
    monkeypatch.setattr(
        daemon_mod.GLib, "timeout_add_seconds",
        lambda delay, _callback: timers.append(delay) or 1,
    )
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)
    instance = make_daemon()
    instance.sessions.map = SimpleNamespace(path=SESSION)
    instance.storage.set_policy("none", allow_prompt=False)
    instance.backfill.request("MAP connected")
    assert timers == []
    instance.storage.set_policy("plaintext", allow_prompt=False)
    instance.backfill.request("MAP connected")
    assert timers == [backfill.BACKFILL_DELAY_SECONDS]
    instance.backfill.stop()
    assert removed == [1]
