"""Call-history storage, seeding, dedupe, retention, and worker scheduling."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from gi.repository import GLib

from blueferry import call_history_sync, config
from blueferry.call_history import INCOMING, MISSED, OUTGOING, CallRecord
from blueferry.call_history_repository import CallHistoryRepository, clear_call_history
from blueferry.call_history_sync import CallHistorySync
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity, StorageUnavailableError

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class _Wallet:
    def __init__(self, key: bytes = b"K" * 32) -> None:
        self.key = key
        self.locked = False

    def get_or_create(self, *, allow_prompt: bool, cancellable=None) -> bytes:
        if self.locked:
            raise StorageUnavailableError("the desktop keyring is locked")
        return self.key

    def delete(self, *, allow_prompt: bool, cancellable=None) -> bool:
        return True


def _call(direction: str, minutes_ago: int, number: str = "15551230001",
          name: str | None = None) -> CallRecord:
    moment = NOW - timedelta(minutes=minutes_ago)
    return CallRecord(
        direction=direction,
        occurred_at=moment,
        raw_time=moment.strftime("%Y%m%dT%H%M%SZ"),
        address=f"+{number}",
        phone=number,
        name=name,
    )


@pytest.fixture
def wallet() -> _Wallet:
    return _Wallet()


@pytest.fixture
def storage(isolated_state, wallet):
    security = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=wallet,
    )
    assert security.status.can_write
    yield security
    security.close()


def _lock(storage: StorageSecurity, wallet: _Wallet) -> None:
    wallet.locked = True
    storage.refresh(allow_prompt=False)
    assert not storage.status.can_read


def _stored_rows() -> int:
    if not config.CALLS_DB.exists():
        return 0
    with closing(sqlite3.connect(config.CALLS_DB)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0])


@pytest.fixture
def harness(storage):
    phone = SimpleNamespace(calls=[], pulls=0)
    jobs, errors, changed, missed = [], [], [], []

    def pull(_sessions):
        phone.pulls += 1
        return list(phone.calls)

    sessions = SimpleNamespace(map=object(), pbap=object(), report_error=errors.append)
    sync = CallHistorySync(
        sessions=sessions,
        storage=storage,
        submit=lambda operation, **handlers: jobs.append((operation, handlers)),
        on_changed=lambda: changed.append(True),
        on_missed=lambda records: missed.append(list(records)),
        pull=pull,
        schedule=lambda *_args: 1,
        cancel=lambda _source: None,
        clock=lambda: NOW,
    )

    def run_jobs():
        while jobs:
            operation, handlers = jobs.pop(0)
            try:
                result = operation()
            except Exception as error:
                handlers["on_error"](error)
            else:
                handlers["on_success"](result)

    yield SimpleNamespace(
        storage=storage, sync=sync, phone=phone, jobs=jobs, run=run_jobs,
        errors=errors, changed=changed, missed=missed, sessions=sessions,
    )
    sync.stop()


# ---- repository ------------------------------------------------------------

def test_rows_are_sealed_and_hold_no_queryable_personal_data(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([_call(MISSED, 5, name="Anna Muster")], now=NOW)

    raw = config.CALLS_DB.read_bytes()
    assert b"15551230001" not in raw and b"Anna" not in raw and b"missed" not in raw
    assert config.CALLS_DB.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(config.CALLS_DB)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
    assert columns == {"id", "payload"}
    assert repository.load(now=NOW) == [_call(MISSED, 5, name="Anna Muster")]


def test_first_sync_seeds_silently_then_only_new_missed_calls_are_reported(storage) -> None:
    repository = CallHistoryRepository(storage)
    backlog = [_call(MISSED, 50), _call(MISSED, 40, "15551230002"), _call(INCOMING, 30)]

    first = repository.replace(backlog, now=NOW)
    second = repository.replace(backlog, now=NOW)
    newer = [_call(MISSED, 1, "15551230003"), *backlog]
    third = repository.replace(newer, now=NOW)
    fourth = repository.replace(newer, now=NOW)

    assert first.seeded and first.new_missed == [] and first.changed
    assert not second.seeded and second.new_missed == [] and not second.changed
    assert third.new_missed == [_call(MISSED, 1, "15551230003")] and third.changed
    assert fourth.new_missed == []


def test_missed_call_is_not_reannounced_after_the_phone_briefly_drops_it(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([], now=NOW)
    repository.replace([_call(MISSED, 5)], now=NOW)

    repository.replace([], now=NOW)  # e.g. a transiently empty mch listing
    again = repository.replace([_call(MISSED, 5)], now=NOW)

    assert again.new_missed == []


def test_retention_drops_expired_calls_and_their_announcement_state(
    storage, monkeypatch,
) -> None:
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 7)
    repository = CallHistoryRepository(storage)
    old = _call(MISSED, 8 * 24 * 60)
    recent = _call(OUTGOING, 60)

    result = repository.replace([recent, old], now=NOW)

    assert result.records == [recent]
    assert repository.load(now=NOW) == [recent]
    # Once the retained row ages out, prune removes it from disk as well.
    assert repository.prune(now=NOW + timedelta(days=8)) == 1
    assert repository.load(now=NOW) == []


def test_clear_erases_rows_and_rearms_silent_seeding(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([], now=NOW)
    clear_call_history()

    result = repository.replace([_call(MISSED, 1)], now=NOW)

    assert result.seeded and result.new_missed == []


def test_wrong_key_fails_closed_instead_of_discarding(storage) -> None:
    CallHistoryRepository(storage).replace([_call(MISSED, 1)], now=NOW)
    other = StorageSecurity(
        settings=SettingsStore(config.SETTINGS_JSON), key_provider=_Wallet(b"X" * 32),
    )
    try:
        assert CallHistoryRepository(other).load(now=NOW) == []
        assert other.status.state == "error"
    finally:
        other.close()
    assert CallHistoryRepository(storage).load(now=NOW) == [_call(MISSED, 1)]


# ---- scheduling and notification ------------------------------------------

def test_sync_runs_on_the_worker_and_seeds_without_notifications(harness) -> None:
    harness.phone.calls = [_call(INCOMING, 20), _call(MISSED, 30)]

    harness.sync.sync()
    assert harness.phone.pulls == 0, "the pull belongs to the submitted job"
    harness.run()

    assert harness.missed == []
    assert harness.changed == [True]
    assert harness.sync.records() == harness.phone.calls


def test_new_missed_calls_are_announced_once_across_syncs(harness) -> None:
    harness.phone.calls = [_call(MISSED, 30)]
    harness.sync.sync()
    harness.run()

    harness.phone.calls = [_call(MISSED, 2, "15551230009"), _call(MISSED, 30)]
    for _ in range(3):
        harness.sync.sync()
        harness.run()

    assert harness.missed == [[_call(MISSED, 2, "15551230009")]]


def test_stale_missed_calls_are_recorded_but_not_announced(harness) -> None:
    harness.sync.sync()
    harness.run()

    harness.phone.calls = [_call(MISSED, 13 * 60)]
    harness.sync.sync()
    harness.run()

    assert harness.missed == []
    assert harness.sync.records() == [_call(MISSED, 13 * 60)]


def test_manual_callers_join_one_pull(harness) -> None:
    results = []
    harness.phone.calls = [_call(OUTGOING, 1)]

    harness.sync.sync(results.append, results.append)
    harness.sync.sync(results.append, results.append)
    harness.sync.refresh()
    assert len(harness.jobs) == 1
    harness.run()

    assert results == [1, 1]
    assert harness.phone.pulls == 1


def test_pull_failure_keeps_the_previous_list_and_reports_the_transport(harness) -> None:
    harness.phone.calls = [_call(OUTGOING, 1)]
    harness.sync.sync()
    harness.run()
    failures = []

    def broken(_sessions):
        raise RuntimeError("transfer failed")

    harness.sync._pull = broken
    harness.sync.sync(failures.append, failures.append)
    harness.run()

    assert [str(error) for error in failures] == ["transfer failed"]
    assert len(harness.errors) == 1
    assert harness.sync.records() == [_call(OUTGOING, 1)]


def test_storage_change_during_sync_discards_the_result(harness, wallet) -> None:
    harness.sync.sync()
    harness.run()  # seed
    harness.phone.calls = [_call(MISSED, 1)]
    harness.sync.sync()
    operation, handlers = harness.jobs.pop()
    result = operation()
    assert _stored_rows() == 1
    wallet.key = b"R" * 32  # the keyring key was replaced mid-sync
    harness.storage.refresh(allow_prompt=False)

    handlers["on_success"](result)

    assert harness.sync.records() == []
    assert harness.missed == [], "nothing sealed under the old key is announced"
    assert harness.errors == [], "a storage race is not a Bluetooth failure"
    assert _stored_rows() == 0


def test_locked_storage_neither_pulls_nor_blames_pbap(harness, wallet) -> None:
    _lock(harness.storage, wallet)
    failures = []

    harness.sync.refresh()
    harness.sync.sync(failures.append, failures.append)

    assert harness.jobs == []
    assert harness.phone.pulls == 0
    assert len(failures) == 1
    assert harness.errors == []


def test_locking_storage_drops_the_in_memory_list(harness, wallet) -> None:
    harness.phone.calls = [_call(OUTGOING, 1)]
    harness.sync.sync()
    harness.run()
    harness.changed.clear()

    _lock(harness.storage, wallet)
    harness.sync.storage_changed()

    assert harness.sync.records() == []
    assert harness.changed == [True]


def test_records_view_applies_retention_without_a_sync(harness, monkeypatch) -> None:
    harness.phone.calls = [_call(OUTGOING, 60)]
    harness.sync.sync()
    harness.run()

    harness.sync._clock = lambda: NOW + timedelta(days=config.HISTORY_RETENTION_DAYS + 1)

    assert harness.sync.records() == []


def test_timers_start_once_pbap_is_live_and_stop_cleanly(storage) -> None:
    scheduled, cancelled = [], []
    sessions = SimpleNamespace(map=None, pbap=None, report_error=lambda _error: None)
    sync = CallHistorySync(
        sessions=sessions, storage=storage,
        submit=lambda *_args, **_kwargs: None,
        on_changed=lambda: None, on_missed=lambda _records: None,
        schedule=lambda delay, callback: scheduled.append(delay) or len(scheduled),
        cancel=cancelled.append,
        interval=300,
    )

    sync.profiles_available()
    assert scheduled == []
    sessions.pbap = object()
    sync.profiles_available()
    sync.profiles_available()
    assert scheduled == [300, call_history_sync.CALL_HISTORY_INITIAL_DELAY_SEC]

    sync.stop()
    assert sorted(cancelled) == [1, 2]
    sync.profiles_available()
    assert len(scheduled) == 2


def test_blocking_pull_runs_on_the_obex_worker_thread(storage, monkeypatch) -> None:
    from blueferry.obex import worker as worker_mod

    monkeypatch.setattr(worker_mod, "initialize_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)
    worker = worker_mod.ObexWorker()
    main = threading.get_ident()
    pulled_on = []
    done = []

    def pull(_sessions):
        pulled_on.append(threading.get_ident())
        return [_call(OUTGOING, 1)]

    sync = CallHistorySync(
        sessions=SimpleNamespace(map=None, pbap=object(), report_error=lambda _e: None),
        storage=storage,
        submit=worker.submit,
        on_changed=lambda: done.append(threading.get_ident()),
        on_missed=lambda _records: None,
        pull=pull,
        schedule=lambda *_args: 1,
        cancel=lambda _source: None,
        clock=lambda: NOW,
    )
    try:
        sync.sync()
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not done and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
    finally:
        worker.shutdown()

    assert pulled_on and pulled_on[0] != main
    # Completion (and anything touching daemon state) is back on the loop.
    assert done == [main]


def test_disabled_config_default_is_off() -> None:
    assert "BLUEFERRY_CALL_HISTORY_ENABLED" in config.LOCAL_ENV_KEYS
    assert config._env_bool("BLUEFERRY_CALL_HISTORY_ENABLED_UNSET_FOR_TEST", False) is False


def test_unlocking_storage_triggers_the_first_sync_only(harness, wallet) -> None:
    _lock(harness.storage, wallet)
    harness.sync.refresh()
    assert harness.jobs == []

    wallet.locked = False
    harness.storage.refresh(allow_prompt=False)
    harness.sync.storage_changed()
    assert len(harness.jobs) == 1
    harness.run()

    harness.sync.storage_changed()
    assert harness.jobs == [], "later storage changes wait for the timer"


# ---- MAP gating (#165), requests, and worker refusals ------------------------

class _Timers:
    """Recording GLib stand-in: callbacks run only when a test fires them."""

    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, object]] = {}
        self.cancelled: list[int] = []
        self._next = 0

    def schedule(self, delay, callback) -> int:
        self._next += 1
        self.pending[self._next] = (delay, callback)
        return self._next

    def cancel(self, source) -> None:
        self.cancelled.append(source)
        self.pending.pop(source, None)

    def delays(self) -> list[int]:
        return sorted(delay for delay, _callback in self.pending.values())

    def fire(self, delay: int) -> None:
        for source, (scheduled, callback) in list(self.pending.items()):
            if scheduled == delay:
                keep = callback()
                if not keep:
                    self.pending.pop(source, None)
                return
        raise AssertionError(f"no timer with delay {delay}")


@pytest.fixture
def gated(storage):
    timers = _Timers()
    phone = SimpleNamespace(calls=[_call(OUTGOING, 1)], pulls=0)
    jobs, errors = [], []
    sessions = SimpleNamespace(map=None, pbap=object(), report_error=errors.append)

    def pull(_sessions):
        phone.pulls += 1
        return list(phone.calls)

    sync = CallHistorySync(
        sessions=sessions, storage=storage,
        submit=lambda operation, **handlers: jobs.append((operation, handlers)),
        on_changed=lambda: None, on_missed=lambda _records: None,
        pull=pull, schedule=timers.schedule, cancel=timers.cancel,
        interval=300, clock=lambda: NOW,
    )

    def run():
        while jobs:
            operation, handlers = jobs.pop(0)
            handlers["on_success"](operation())

    yield SimpleNamespace(
        sync=sync, sessions=sessions, timers=timers, jobs=jobs, run=run,
        phone=phone, errors=errors,
    )
    sync.stop()


GRACE = call_history_sync.CALL_HISTORY_MAP_GRACE_SECONDS
INITIAL = call_history_sync.CALL_HISTORY_INITIAL_DELAY_SEC
REQUEST = call_history_sync.CALL_HISTORY_REQUEST_DELAY_SEC


def test_no_automatic_pull_before_map_or_its_grace_period(gated) -> None:
    gated.sync.profiles_available()
    gated.timers.fire(INITIAL)
    gated.timers.fire(300)

    assert gated.jobs == []
    assert gated.sync.deferred
    assert GRACE in gated.timers.delays()


def test_pbap_only_setup_pulls_once_after_the_grace_period(gated) -> None:
    gated.sync.profiles_available()
    gated.timers.fire(INITIAL)

    gated.timers.fire(GRACE)
    assert len(gated.jobs) == 1
    gated.run()
    # MAP never connected: the periodic tick stays off.
    gated.timers.fire(300)
    assert gated.jobs == []
    assert gated.phone.pulls == 1


def test_map_arrival_catches_up_a_deferred_pull(gated) -> None:
    gated.sync.profiles_available()
    gated.timers.fire(INITIAL)
    assert gated.jobs == []

    gated.sessions.map = object()
    gated.sync.profiles_available()

    assert len(gated.jobs) == 1
    assert not gated.sync.deferred


def test_every_automatic_pull_yields_while_map_reconnects(gated) -> None:
    gated.sessions.map = object()
    gated.sync.profiles_available()
    gated.timers.fire(INITIAL)
    gated.run()
    assert gated.phone.pulls == 1

    gated.sessions.map = None  # MAP dropped; its retries need the worker
    gated.timers.fire(300)
    gated.sync.request_sync("test")
    gated.timers.fire(REQUEST)
    gated.sync.profiles_available()  # PBAP-only partial readiness
    gated.timers.fire(INITIAL)
    assert gated.jobs == []

    gated.sessions.map = object()
    gated.sync.profiles_available()
    assert len(gated.jobs) == 1


def test_explicit_client_sync_is_never_gated(gated) -> None:
    results = []

    gated.sync.sync(results.append, results.append)
    gated.run()

    assert results == [1]


def test_requests_coalesce_and_one_follow_up_runs_after_a_busy_pull(gated) -> None:
    gated.sessions.map = object()
    gated.sync.request_sync("call ended")
    gated.sync.request_sync("call ended")
    assert gated.timers.delays() == [REQUEST]

    gated.timers.fire(REQUEST)
    assert len(gated.jobs) == 1
    # Two more requests while that pull runs: exactly one follow-up.
    gated.sync.request_sync("call ended")
    gated.timers.fire(REQUEST)
    gated.sync.request_sync("call ended")
    gated.timers.fire(REQUEST)
    assert len(gated.jobs) == 1
    gated.run()

    assert gated.phone.pulls == 2
    assert gated.jobs == []


def test_stop_cancels_grace_and_request_timers(gated) -> None:
    gated.sync.profiles_available()
    gated.timers.fire(INITIAL)
    gated.sync.request_sync("test")

    gated.sync.stop()

    assert gated.timers.pending == {}
    gated.sync.request_sync("late")
    assert gated.timers.pending == {}


def test_worker_refusal_is_not_blamed_on_pbap(gated, caplog) -> None:
    def refuse(*_args, **_kwargs):
        raise RuntimeError("Bluetooth recovery is in progress")

    gated.sync._submit = refuse
    failures = []

    with caplog.at_level("INFO"):
        gated.sync.sync(failures.append, failures.append)

    assert [str(error) for error in failures] == ["Bluetooth recovery is in progress"]
    assert gated.errors == []
    assert not gated.sync.pending
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_pull_uses_three_bounded_listings_in_order(monkeypatch) -> None:
    from blueferry import contacts as contacts_module
    from blueferry.limits import MAX_CALL_HISTORY_BYTES, MAX_CALL_HISTORY_PER_FOLDER

    calls = []

    def listing(sessions, phonebook, **kwargs):
        calls.append((phonebook, kwargs))
        return (
            "BEGIN:VCARD\nTEL:+15551230001\n"
            "X-IRMC-CALL-DATETIME:20260928T120000Z\nEND:VCARD\n"
        )

    monkeypatch.setattr(contacts_module, "pull_vcard_listing", listing)

    records = call_history_sync.pull_call_history(SimpleNamespace())

    assert [phonebook for phonebook, _kwargs in calls] == ["ich", "och", "mch"]
    for _phonebook, kwargs in calls:
        assert kwargs == {
            "max_entries": MAX_CALL_HISTORY_PER_FOLDER,
            "max_bytes": MAX_CALL_HISTORY_BYTES,
            "allow_empty": True,
            "overall_timeout_s": call_history_sync.CALL_HISTORY_TRANSFER_MAX_SECONDS,
        }
    # Folder direction fills the missing type; ich and mch collapse to missed.
    assert sorted(record.direction for record in records) == [MISSED, OUTGOING]


def test_a_failing_folder_aborts_the_whole_pull(monkeypatch) -> None:
    from blueferry import contacts as contacts_module

    calls = []

    def listing(sessions, phonebook, **kwargs):
        calls.append(phonebook)
        if phonebook == "och":
            raise RuntimeError("transfer failed")
        return ""

    monkeypatch.setattr(contacts_module, "pull_vcard_listing", listing)

    with pytest.raises(RuntimeError, match="transfer failed"):
        call_history_sync.pull_call_history(SimpleNamespace())
    assert calls == ["ich", "och"]


# ---- repository write minimization -----------------------------------------

def _row_state():
    with closing(sqlite3.connect(config.CALLS_DB)) as connection:
        rows = connection.execute("SELECT id, payload FROM calls ORDER BY id").fetchall()
        state = connection.execute("SELECT payload FROM state").fetchall()
    return rows, state


def test_unchanged_poll_rewrites_nothing(storage) -> None:
    repository = CallHistoryRepository(storage)
    calls = [_call(MISSED, 5), _call(INCOMING, 10)]
    repository.replace(calls, now=NOW)
    before = _row_state()

    result = repository.replace(calls, now=NOW)

    # Fresh AES-GCM nonces would change every payload on any rewrite.
    assert _row_state() == before
    assert not result.changed


def test_new_missed_call_rewrites_rows_and_state(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([_call(INCOMING, 10)], now=NOW)
    rows, state = _row_state()

    repository.replace([_call(MISSED, 1), _call(INCOMING, 10)], now=NOW)

    new_rows, new_state = _row_state()
    assert len(new_rows) == 2 and new_rows != rows
    assert new_state != state


def test_unusable_announcement_state_seeds_again_silently(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([], now=NOW)
    with closing(sqlite3.connect(config.CALLS_DB)) as connection, connection:
        connection.execute(
            "UPDATE state SET payload = ? WHERE name = 'seen'",
            (repository._seal(["not", "a", "mapping"], "call-state-v1"),),
        )

    result = repository.replace([_call(MISSED, 1)], now=NOW)

    assert result.seeded and result.new_missed == []
