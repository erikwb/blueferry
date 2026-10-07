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
    phone = SimpleNamespace(calls=[], pulls=0, listings=[])
    jobs, errors, changed, missed = [], [], [], []

    def pull(_sessions, phonebook, direction):
        phone.listings.append(phonebook)
        if phonebook == "mch":  # the last listing of every sync
            phone.pulls += 1
        return [call for call in phone.calls if call.direction == direction]

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


def test_empty_first_answer_keeps_silent_seeding_armed(storage) -> None:
    """An empty first listing must not turn the real backlog into new calls."""
    repository = CallHistoryRepository(storage)
    backlog = [_call(MISSED, 30), _call(MISSED, 20, "15551230002")]

    empty = repository.replace([], now=NOW)
    first_real = repository.replace(backlog, now=NOW)
    later = repository.replace([_call(MISSED, 1, "15551230003"), *backlog], now=NOW)

    assert empty.seeded and empty.new_missed == []
    assert first_real.seeded and first_real.new_missed == []
    assert [record.phone for record in later.new_missed] == ["15551230003"]


def _floating(direction: str, wall: str, number: str = "15551230001") -> CallRecord:
    """A call as the phone renders it: floating local time, no zone."""
    moment = datetime.strptime(wall, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return CallRecord(
        direction=direction, occurred_at=moment, raw_time=wall,
        address=f"+{number}", phone=number, name=None,
    )


def test_phone_timezone_change_does_not_reannounce_missed_calls(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([_floating(INCOMING, "20260928T080000")], now=NOW)
    before = [
        _floating(MISSED, "20260928T113010"),
        _floating(MISSED, "20260928T114500", "15551230002"),
    ]
    announced = repository.replace(before, now=NOW).new_missed
    assert {record.raw_time for record in announced} == {
        "20260928T113010", "20260928T114500",
    }
    # The phone travelled to UTC-05:30 (or crossed DST): same calls, new text.
    after = [
        _floating(MISSED, "20260928T060010"),
        _floating(MISSED, "20260928T061500", "15551230002"),
    ]

    result = repository.replace(after, now=NOW)

    assert result.new_missed == []


def test_new_call_from_the_same_number_is_still_announced(storage) -> None:
    repository = CallHistoryRepository(storage)
    first = _floating(MISSED, "20260928T100000")
    repository.replace([first], now=NOW)
    # Exactly two hours later, same number, same minute and second: the
    # earlier call is still listed, so this is not a re-rendering.
    second = _floating(MISSED, "20260928T120000")

    result = repository.replace([second, first], now=NOW)

    assert result.new_missed == [second]


def test_a_different_phone_resets_the_mirror_and_seeds_silently(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([_call(MISSED, 30)], now=NOW, phone="AA:BB:CC:DD:EE:01")
    repository.replace([_call(MISSED, 30)], now=NOW, phone="aa:bb:cc:dd:ee:01")
    other = [_call(MISSED, 5, "15551239999"), _call(OUTGOING, 3, "15551239998")]

    result = repository.replace(other, now=NOW, phone="AA:BB:CC:DD:EE:02")

    assert result.seeded and result.new_missed == []
    assert repository.load(now=NOW) == sorted(
        other, key=lambda record: record.occurred_at, reverse=True,
    )
    newer = repository.replace(
        [_call(MISSED, 1, "15551230005"), *other], now=NOW, phone="AA:BB:CC:DD:EE:02",
    )
    assert [record.phone for record in newer.new_missed] == ["15551230005"]


def test_forgetting_the_phone_rearms_silent_seeding(storage) -> None:
    repository = CallHistoryRepository(storage)
    repository.replace([_call(MISSED, 30)], now=NOW)

    repository.forget_announcements()
    result = repository.replace([_call(MISSED, 2, "15551230002"), _call(MISSED, 30)], now=NOW)

    assert result.seeded and result.new_missed == []
    assert len(repository.load(now=NOW)) == 2


def test_partial_missed_pull_keeps_incoming_and_outgoing_calls(storage) -> None:
    repository = CallHistoryRepository(storage)
    incoming, outgoing = _call(INCOMING, 50), _call(OUTGOING, 40, "15551230002")
    old_missed = _call(MISSED, 30, "15551230003")
    repository.replace([incoming, outgoing, old_missed], now=NOW)
    new_missed = _call(MISSED, 2, "15551230004")

    result = repository.replace(
        [new_missed], now=NOW, directions=frozenset({MISSED}),
    )

    assert result.records == [new_missed, outgoing, incoming]
    assert result.new_missed == [new_missed]


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

    def broken(_sessions, _phonebook, _direction):
        raise RuntimeError("org.freedesktop.DBus.Error.UnknownObject: transfer gone")

    harness.sync._pull = broken
    harness.sync.sync(failures.append, failures.append)
    harness.run()

    assert [str(error) for error in failures] == [
        "org.freedesktop.DBus.Error.UnknownObject: transfer gone"
    ]
    assert len(harness.errors) == 1
    assert harness.sync.records() == [_call(OUTGOING, 1)]


def test_storage_change_during_sync_discards_the_result(harness, wallet) -> None:
    harness.sync.sync()
    harness.run()  # seed
    harness.phone.calls = [_call(MISSED, 1)]
    harness.sync.sync()
    while True:  # run the listings; stop at the final store job
        operation, handlers = harness.jobs.pop(0)
        result = operation()
        if not isinstance(result, list):
            break
        handlers["on_success"](result)
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


@pytest.mark.real_glib_sources
def test_blocking_pull_runs_on_the_obex_worker_thread(storage, monkeypatch) -> None:
    from blueferry.obex import worker as worker_mod

    monkeypatch.setattr(worker_mod, "initialize_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)
    worker = worker_mod.ObexWorker()
    main = threading.get_ident()
    pulled_on = []
    done = []

    def pull(_sessions, _phonebook, direction):
        pulled_on.append(threading.get_ident())
        return [_call(OUTGOING, 1)] if direction == OUTGOING else []

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

    assert len(pulled_on) == 3 and main not in pulled_on
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
    phone = SimpleNamespace(calls=[_call(OUTGOING, 1)], pulls=0, listings=[])
    jobs, errors = [], []
    sessions = SimpleNamespace(map=None, pbap=object(), report_error=errors.append)

    def pull(_sessions, phonebook, direction):
        phone.listings.append(phonebook)
        if phonebook == "mch":
            phone.pulls += 1
        return [call for call in phone.calls if call.direction == direction]

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

    def run_failing():
        while jobs:
            operation, handlers = jobs.pop(0)
            try:
                result = operation()
            except Exception as error:
                handlers["on_error"](error)
            else:
                handlers["on_success"](result)

    yield SimpleNamespace(
        sync=sync, sessions=sessions, timers=timers, jobs=jobs, run=run,
        run_failing=run_failing,
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


# ---- one listing per worker job, missed-only polling, error handling --------

def test_each_listing_is_its_own_worker_job(harness) -> None:
    """A MAP job queued meanwhile waits for one listing, not a whole sync."""
    harness.phone.calls = [_call(OUTGOING, 1)]
    harness.sync.sync()
    order = []

    for expected in ("ich", "och", "mch"):
        assert len(harness.jobs) == 1
        operation, handlers = harness.jobs.pop(0)
        handlers["on_success"](operation())
        order.append(harness.phone.listings[-1])
        assert order[-1] == expected
    assert len(harness.jobs) == 1, "then one short store job"
    harness.run()

    assert harness.sync.records() == [_call(OUTGOING, 1)]


def test_one_job_stays_below_the_client_obex_timeout() -> None:
    from blueferry.protocol import OBEX_CALL_TIMEOUT_SEC

    # PBAP Select (10 s) + PullAll (30 s) + transfer + 2 s file grace.
    worst = 10 + 30 + call_history_sync.CALL_HISTORY_TRANSFER_MAX_SECONDS + 2
    assert worst < OBEX_CALL_TIMEOUT_SEC / 2


def test_periodic_poll_lists_only_missed_calls_after_the_first_full_sync(gated) -> None:
    gated.sessions.map = object()
    gated.phone.calls = [_call(OUTGOING, 10), _call(INCOMING, 20)]
    gated.sync.profiles_available()
    gated.timers.fire(300)  # first automatic pull: full
    gated.run()
    assert gated.phone.listings == ["ich", "och", "mch"]

    gated.phone.calls.append(_call(MISSED, 1))
    gated.timers.fire(300)
    gated.run()

    assert gated.phone.listings[3:] == ["mch"]
    assert gated.sync.records() == [_call(MISSED, 1), _call(OUTGOING, 10), _call(INCOMING, 20)]


def test_explicit_sync_during_a_missed_only_poll_gets_a_full_pull(gated) -> None:
    gated.sessions.map = object()
    gated.sync.sync()
    gated.run()
    gated.phone.listings.clear()
    gated.sync.profiles_available()
    gated.timers.fire(300)  # mch-only poll is now running
    results = []

    gated.sync.sync(results.append, results.append)
    gated.run()

    assert gated.phone.listings == ["mch", "ich", "och", "mch"]
    assert results == [1]


def test_requests_coalesce_to_a_full_pull_if_any_asked_for_one(gated) -> None:
    gated.sessions.map = object()
    gated.sync.sync()
    gated.run()
    gated.phone.listings.clear()

    gated.sync.request_sync("ancs missed call", full=False)
    gated.sync.request_sync("ancs call ended", full=True)
    gated.timers.fire(REQUEST)
    gated.run()
    gated.sync.request_sync("ancs missed call", full=False)
    gated.timers.fire(REQUEST)
    gated.run()

    assert gated.phone.listings == ["ich", "och", "mch", "mch"]


@pytest.mark.parametrize(("message", "reported"), [
    ("org.freedesktop.DBus.Error.NoReply: Did not receive a reply", False),
    ("transfer timed out", False),
    ("org.freedesktop.DBus.Error.UnknownObject: no such session", True),
    ("org.freedesktop.DBus.Error.ServiceUnknown: obexd is gone", True),
])
def test_only_a_vanished_session_is_reported_never_a_timeout(
    harness, message, reported,
) -> None:
    def broken(_sessions, _phonebook, _direction):
        raise RuntimeError(message)

    harness.sync._pull = broken
    harness.sync.sync()
    harness.run()

    assert bool(harness.errors) is reported
    assert not harness.sync.pending


def test_failed_automatic_pulls_back_off_exponentially(gated) -> None:
    gated.sessions.map = object()
    gated.sync.sync()
    gated.run()
    gated.sync.profiles_available()
    attempts = []

    def broken(_sessions, phonebook, _direction):
        attempts.append(phonebook)
        raise RuntimeError("transfer timed out")

    gated.sync._pull = broken
    ticks = []
    for tick in range(1, 12):
        before = len(attempts)
        gated.timers.fire(300)
        gated.run_failing()
        if len(attempts) > before:
            ticks.append(tick)

    # Failure n skips 2**n - 1 ticks (capped).
    assert ticks == [1, 3, 7]
    gated.sync._pull = None


def test_sync_seals_through_a_following_snapshot(harness, monkeypatch) -> None:
    calls = []
    original = harness.storage.snapshot

    def snapshot(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(harness.storage, "snapshot", snapshot)
    harness.sync.sync()
    harness.run()

    assert calls == [{"follow": True}]


def test_a_different_paired_phone_seeds_silently(storage) -> None:
    def make(phone, calls, missed):
        jobs = []
        sync = CallHistorySync(
            sessions=SimpleNamespace(map=object(), pbap=object(), report_error=lambda _e: None),
            storage=storage,
            submit=lambda operation, **handlers: jobs.append((operation, handlers)),
            on_changed=lambda: None,
            on_missed=missed.append,
            pull=lambda _s, _p, direction: [c for c in calls if c.direction == direction],
            schedule=lambda *_args: 1, cancel=lambda _source: None,
            clock=lambda: NOW, phone=phone,
        )
        sync.sync()
        while jobs:
            operation, handlers = jobs.pop(0)
            handlers["on_success"](operation())
        sync.stop()

    missed = []
    make("AA:BB:CC:DD:EE:01", [_call(MISSED, 30)], missed)
    make("AA:BB:CC:DD:EE:02", [_call(MISSED, 2, "15551230002")], missed)

    assert missed == []


def test_stopping_mid_sync_abandons_the_chain_and_keeps_nothing(harness) -> None:
    harness.phone.calls = [_call(OUTGOING, 1)]
    harness.sync.sync()
    operation, handlers = harness.jobs.pop(0)
    harness.sync.stop()
    handlers["on_success"](operation())

    assert harness.jobs == []
    assert not harness.sync.pending
    assert _stored_rows() == 0


def test_a_store_that_lands_after_opt_out_is_erased(harness) -> None:
    harness.phone.calls = [_call(OUTGOING, 1)]
    harness.sync.sync()
    while len(harness.phone.listings) < 3:
        operation, handlers = harness.jobs.pop(0)
        handlers["on_success"](operation())
    store, handlers = harness.jobs.pop(0)  # already handed to the worker

    harness.sync.disable()
    handlers["on_success"](store())

    assert _stored_rows() == 0
    assert harness.sync.records() == []


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
    repository.replace([_call(INCOMING, 10)], now=NOW)
    with closing(sqlite3.connect(config.CALLS_DB)) as connection, connection:
        connection.execute(
            "UPDATE state SET payload = ? WHERE name = 'seen'",
            (repository._seal(["not", "a", "mapping"], "call-state-v1"),),
        )

    result = repository.replace([_call(MISSED, 1)], now=NOW)

    assert result.seeded and result.new_missed == []
