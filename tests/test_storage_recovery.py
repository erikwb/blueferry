"""Late keyring availability must recover without a client or password prompt."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from gi.repository import GLib

from blueferry import config
from blueferry import contact_repository as repository_mod
from blueferry import contacts as contacts_mod
from blueferry import daemon as daemon_mod
from blueferry import history as history_mod
from blueferry import storage_preparation as preparation_mod
from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.call_history import MISSED, CallRecord
from blueferry.call_history_repository import CallHistoryRepository, clear_call_history
from blueferry.contact_repository import ContactRepository
from blueferry.contact_sync import CONTACTS_REFRESH_SEC
from blueferry.contacts import ContactsResolver, clear_contact_cache
from blueferry.errors import ConfigurationError, ConfirmationRequiredError
from blueferry.events import SmsEvent
from blueferry.history import append_event, clear_events, read_events
from blueferry.settings_store import SettingsStore
from blueferry.sinks.sqlite import SqliteSink
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_security import (
    DAMAGED_DATABASE_DETAIL,
    KEY_CHECK_SETTING,
    KEY_MISMATCH_DETAIL,
    KEY_MISSING_DETAIL,
    StorageKeyMissingError,
    StorageSecurity,
    StorageStatus,
    StorageUnavailableError,
)


class _Wallet:
    """``key`` None is a keyring without BlueFerry's item, e.g. another wallet."""

    locked = True
    key: bytes | None = b"K" * 32
    created = 0
    deleted = 0

    def get_or_create(self, *, allow_prompt, cancellable=None, create=True):
        if self.locked and not allow_prompt:
            raise StorageUnavailableError("the desktop keyring is locked")
        if self.key is None:
            if not create:
                raise StorageKeyMissingError(KEY_MISSING_DETAIL)
            self.created += 1
            self.key = bytes([self.created]) * 32
        return self.key

    def delete(self, *, allow_prompt, cancellable=None):
        self.deleted += 1
        existed, self.key = self.key is not None, None
        return existed


class _Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, operation, **handlers):
        self.jobs.append((operation, handlers))

    def shutdown(self, **_kwargs):
        self.jobs.clear()

    def finish(self):
        operation, handlers = self.jobs.pop(0)
        try:
            value = operation()
        except Exception as error:
            handlers["on_error"](error)
        else:
            handlers["on_success"](value)


@pytest.fixture
def recovery(make_daemon, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 30)
    scheduled = []
    monkeypatch.setattr(
        GLib, "timeout_add_seconds", lambda delay, _callback: scheduled.append(delay) or 1,
    )
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)
    wallet = _Wallet()
    monkeypatch.setattr(daemon_mod, "ObexWorker", _Queue)
    monkeypatch.setattr(
        daemon_mod, "StorageSecurity",
        lambda **kwargs: StorageSecurity(key_provider=wallet, **kwargs),
    )
    daemon = make_daemon()
    daemon.sessions.pbap = object()  # PBAP is live; MAP is not.
    monkeypatch.setattr(daemon.sessions, "report_error", lambda _error: None)
    storage = daemon.storage
    published = []
    monkeypatch.setattr(daemon, "_emit_status", lambda: published.append(storage.status))
    operations = BackendOperations(daemon.sessions, BackendDependencies(
        storage=storage, contacts=daemon.contacts,
        prepare_storage=preparation_mod.prepare_storage,
        on_storage_prepared=daemon._apply_storage_preparation,
        on_storage_changed=daemon._on_storage_changed,
    ))
    queue = _Queue()
    outcomes = []

    def unlock(*, interactive=False):
        method = operations.change_storage_async if interactive else operations.retry_storage_async
        method(queue.submit, outcomes.append, lambda error: pytest.fail(str(error)))
        queue.finish()

    return SimpleNamespace(
        storage=storage, wallet=wallet, daemon=daemon, operations=operations,
        queue=queue, outcomes=outcomes, published=published, unlock=unlock,
        scheduled=scheduled,
    )


def test_late_keyring_restores_contacts_history_and_message_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda *_args: 1)
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)

    class Wallet:
        def __init__(self):
            self.locked = False
            self.calls = []

        def get_or_create(self, *, allow_prompt, cancellable=None, create=True):
            self.calls.append(allow_prompt)
            if self.locked:
                raise StorageUnavailableError("the desktop keyring is locked")
            return b"K" * 32

    wallet = Wallet()
    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=wallet,
    )
    ContactRepository(storage).replace([("Alice", ["15551111111"], [])])
    append_event({
        "kind": "sms_received", "handle": "old", "sender_address": "+15551111111",
        "body": "retained before restart",
    }, storage=storage)
    storage.close()
    wallet.locked = True
    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=wallet,
    )
    contacts = ContactsResolver(storage=storage)
    sink = SqliteSink(storage=storage)
    status_changes = []
    operations = BackendOperations(SimpleNamespace(), BackendDependencies(
        storage=storage, contacts=contacts,
        on_storage_changed=lambda: status_changes.append(storage.status),
    ))
    assert contacts.resolve("+15551111111") is None
    assert operations.list_threads(10) == []

    pending = []
    outcomes = []

    def submit(operation, **handlers):
        pending.append((operation, handlers))

    def finish():
        operation, handlers = pending.pop()
        try:
            value = operation()
        except Exception as error:
            handlers["on_error"](error)
        else:
            handlers["on_success"](value)

    def retry():
        operations.retry_storage_async(submit, outcomes.append, lambda e: pytest.fail(str(e)))

    retry()
    assert storage.busy
    retry()  # A poll cannot overlap an in-flight wallet request.
    assert len(pending) == 1
    finish()
    assert storage.status.state == "locked"
    assert not status_changes
    assert not outcomes

    wallet.locked = False
    retry()
    assert storage.status.state == "locked"  # Worker results apply on completion only.
    finish()
    assert storage.status.can_write
    assert len(status_changes) == 1
    assert outcomes[0]["storage_state"] == "ready"
    assert contacts.resolve("+15551111111") == "Alice"
    assert operations.list_threads(10)[0]["messages"][0]["body"] == "retained before restart"

    sink.handle(SmsEvent(
        kind="sms_received", handle="new", sender_address="+15551111111",
        sender_phone_norm="15551111111", contact_name="Alice",
        body="received after keyring recovery", timestamp=None, is_read=False,
    ))
    assert [e["handle"] for e in read_events(storage=storage)] == ["old", "new"]
    assert b"received after keyring recovery" not in config.EVENTS_DB.read_bytes()
    retry()
    assert not pending
    assert not any(wallet.calls)
    storage.close()


@pytest.mark.parametrize(("policy", "state", "busy"), [
    ("encrypted", "ready", False),
    ("encrypted", "error", False),
    ("encrypted", "locked", True),
    ("plaintext", "ready", False),
    ("none", "disabled", False),
])
def test_passive_retry_respects_policy_pending_unlocks_and_corruption(policy, state, busy):
    storage = SimpleNamespace(status=StorageStatus(policy, state, ""), busy=busy)
    operations = BackendOperations(SimpleNamespace(), BackendDependencies(storage=storage))
    operations.retry_storage_async(
        lambda *_args, **_kwargs: pytest.fail("unexpected wallet request"),
        lambda _status: pytest.fail("unexpected storage update"),
        lambda error: pytest.fail(str(error)),
    )


@pytest.mark.parametrize("interactive", [False, True])
def test_recovery_runs_deferred_history_cleanup_and_seeds_ancs(recovery, interactive):
    r = recovery
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    now = datetime.now(timezone.utc)
    for event in [
        {"kind": "sms_received", "handle": "expired", "body": "expired",
         "seen_at": (now - timedelta(days=90)).isoformat()},
        {"kind": "sms_received", "handle": "recent", "body": "keep me",
         "seen_at": now.isoformat()},
        {"kind": "ancs_notification", "app_id": MESSAGES_APP_ID,
         "notification_id": 42, "body": "correlation", "extra_private_field": "discard",
         "seen_at": now.isoformat()},
        {"kind": "ancs_notification", "app_id": "unrelated.app", "body": "discard",
         "seen_at": now.isoformat()},
    ]:
        append_event(event, storage=r.storage)
    append_event({"kind": "sms_received", "body": "legacy plaintext"})
    r.wallet.locked = True
    r.storage.refresh(allow_prompt=False)
    r.daemon.contacts.refresh()
    SqliteSink(storage=r.storage)

    r.wallet.locked = False
    r.unlock(interactive=interactive)

    retained = read_events(storage=r.storage)
    assert len(retained) == 2
    assert retained[0]["handle"] == "recent"
    assert "extra_private_field" not in retained[1]
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert r.published[-1].state == "ready"
    assert b"legacy plaintext" not in config.EVENTS_DB.read_bytes()


@pytest.mark.parametrize("interactive", [False, True])
def test_recovery_rejects_replaced_key_before_announcing_ready_or_writing(recovery, interactive):
    r = recovery
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    append_event({"kind": "sms_received", "handle": "original", "body": "keep"}, storage=r.storage)
    before = config.EVENTS_DB.read_bytes()
    r.wallet.locked = True
    r.storage.refresh(allow_prompt=False)
    r.daemon.contacts.refresh()
    sink = SqliteSink(storage=r.storage)
    r.wallet.key = b"L" * 32
    r.wallet.locked = False

    r.unlock(interactive=interactive)

    assert r.storage.status.state == "error"
    assert r.outcomes[-1]["storage_state"] == "error"
    assert r.published[-1].state == "error"
    assert not r.daemon.obex_worker.jobs
    sink.handle(SmsEvent(
        kind="sms_received", handle="new", sender_address="+15551111111",
        sender_phone_norm="15551111111", contact_name=None,
        body="must not mix keys", timestamp=None, is_read=False,
    ))
    assert config.EVENTS_DB.read_bytes() == before
    r.operations.retry_storage_async(r.queue.submit, r.outcomes.append, lambda _error: None)
    assert not r.queue.jobs

    # The original ciphertext still works when the correct key is restored.
    r.wallet.key = b"K" * 32
    r.unlock(interactive=True)
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert read_events(storage=r.storage)[0]["handle"] == "original"


@pytest.mark.parametrize("cached", [False, True])
def test_keyring_recovery_resumes_initial_phonebook_sync(recovery, monkeypatch, cached):
    r = recovery
    r.daemon.sessions.map = object()
    r.daemon.listener = object()
    if cached:
        r.wallet.locked = False
        r.storage.refresh(allow_prompt=False)
        ContactRepository(r.storage).replace([("Alice", ["15551111111"], [])])
        r.wallet.locked = True
    r.storage.refresh(allow_prompt=False)
    r.daemon.contacts.refresh()
    r.daemon._post_available_sessions_setup()
    assert not r.daemon.obex_worker.jobs  # Defer pulls while persistence is unavailable.
    assert CONTACTS_REFRESH_SEC in r.scheduled  # The daily refresh is armed regardless.

    def pull(_sessions, *, storage, **_options):
        assert storage.status.can_write
        return ContactRepository(storage).replace([("Alice", ["15551111111"], [])])

    monkeypatch.setattr(contacts_mod, "pull_phonebook", pull)
    r.wallet.locked = False
    r.unlock()
    if cached:
        assert not r.daemon.obex_worker.jobs
    else:
        assert len(r.daemon.obex_worker.jobs) == 1
        r.daemon._on_storage_changed()
        assert len(r.daemon.obex_worker.jobs) == 1  # No overlapping pulls.
        r.daemon.obex_worker.finish()
    assert r.daemon.contacts.resolve("+15551111111") == "Alice"
    r.daemon._on_storage_changed()
    assert not r.daemon.obex_worker.jobs


@pytest.mark.parametrize('previous_pull', ['complete', 'late_success', 'late_failure'])
def test_policy_change_resyncs_an_empty_cache_despite_an_older_pull(
    recovery, monkeypatch, previous_pull,
):
    r = recovery
    r.daemon.sessions.map = object()
    r.daemon.listener = object()
    records = [('Alice', [], [])]  # Valid contact, but zero usable destinations.

    def pull(_sessions, *, storage, **_options):
        return ContactRepository(storage).replace(records)

    monkeypatch.setattr(contacts_mod, 'pull_phonebook', pull)
    r.wallet.locked = False
    r.unlock()
    if previous_pull == 'complete':
        r.daemon.obex_worker.finish()
        r.daemon._post_available_sessions_setup()
        assert not r.daemon.obex_worker.jobs
    else:
        operation, old_callbacks = r.daemon.obex_worker.jobs.pop(0)
        if previous_pull == 'late_success':
            pulled = operation()  # Delay only the GLib completion callback.

    r.operations.change_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)),
        policy='plaintext',
    )
    r.queue.finish()
    assert r.outcomes[-1]['storage_state'] == 'ready'
    assert r.daemon.contacts.records() == []  # Changing policy cleared the archive.
    if previous_pull != 'complete':
        assert not r.daemon.obex_worker.jobs
        if previous_pull == 'late_success':
            old_callbacks['on_success'](pulled)
        else:
            old_callbacks['on_error'](RuntimeError('old download failed'))

    assert len(r.daemon.obex_worker.jobs) == 1
    r.daemon.obex_worker.finish()
    assert r.daemon.contacts.records() == records
    assert r.daemon.contacts.count() == 0
    r.daemon._post_available_sessions_setup()
    r.daemon._on_storage_changed()
    assert not r.daemon.obex_worker.jobs


def test_preparation_failure_does_not_publish_writable_storage(recovery, monkeypatch):
    def fail(**_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(preparation_mod, "prune_events", fail)
    recovery.wallet.locked = False
    recovery.unlock()
    assert recovery.outcomes[-1]["storage_state"] == "locked"
    assert recovery.published[-1].state == "locked"
    assert not recovery.daemon.obex_worker.jobs


def test_contact_queue_failure_does_not_hide_storage_recovery(recovery, monkeypatch):
    r = recovery
    r.daemon.sessions.map = object()
    submit = r.daemon.obex_worker.submit

    def full(*_args, **_kwargs):
        raise RuntimeError("OBEX operation queue is full")

    monkeypatch.setattr(r.daemon.obex_worker, "submit", full)
    r.wallet.locked = False
    r.unlock()
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert r.published[-1].state == "ready"
    assert not r.daemon.contact_sync.pending
    monkeypatch.setattr(r.daemon.obex_worker, "submit", submit)
    r.daemon.contact_sync.refresh()
    assert len(r.daemon.obex_worker.jobs) == 1


def test_temporary_database_lock_recovers_on_the_next_poll(recovery, monkeypatch):
    r = recovery
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    append_event({"kind": "sms_received", "body": "retained"}, storage=r.storage)
    r.storage.require_preparation()
    original_open = history_mod._open_database

    def short_wait(path=None):
        connection = original_open(path)
        connection.execute("PRAGMA busy_timeout = 1")
        return connection

    monkeypatch.setattr(history_mod, "_open_database", short_wait)
    other = sqlite3.connect(config.EVENTS_DB)
    try:
        other.execute("BEGIN IMMEDIATE")
        r.unlock()
        assert r.storage.status.state == "locked"
        assert not r.storage.status.can_write
        assert not r.daemon.obex_worker.jobs
    finally:
        other.rollback()
        other.close()
    r.unlock()
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert read_events(storage=r.storage)[0]["body"] == "retained"


@pytest.mark.parametrize("policy", ["encrypted", "plaintext", "none"])
def test_initial_preparation_handles_every_retention_policy(recovery, policy):
    r = recovery
    # Seed legacy data to prove that no-storage startup still erases it.
    append_event({"kind": "sms_received", "body": "legacy"})
    settings = SettingsStore(r.storage.settings_path)
    settings.update(local_data=policy)
    r.storage._policy = policy
    r.storage.require_preparation()
    r.wallet.locked = False
    r.operations.retry_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)), initialize=True,
    )
    assert not r.storage.status.can_write
    r.queue.finish()
    expected = "disabled" if policy == "none" else "ready"
    assert r.outcomes[-1]["storage_state"] == expected
    if policy != "plaintext":
        assert read_events() == []


@pytest.mark.parametrize("error", [
    PermissionError("access denied"), sqlite3.DatabaseError("database disk image is malformed"),
])
def test_nontransient_preparation_errors_do_not_retry(recovery, monkeypatch, error):
    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(preparation_mod, "prune_events", fail)
    recovery.wallet.locked = False
    recovery.unlock()
    assert recovery.outcomes[-1]["storage_state"] == "error"
    recovery.operations.retry_storage_async(
        recovery.queue.submit, recovery.outcomes.append, lambda _error: None,
    )
    assert not recovery.queue.jobs


@pytest.mark.parametrize("locked", [False, True])
def test_startup_preserves_legacy_preference_privacy(recovery, locked):
    r = recovery
    path = r.storage.settings_path
    key = "address:phone:15551111111"
    StarredThreadsStore(path).set_starred(key, True)
    r.wallet.locked = locked
    r.operations.retry_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)), initialize=True,
    )
    r.queue.finish()
    assert key not in path.read_text()
    if not locked:
        assert StarredThreadsStore(path, storage=r.storage).keys() == [key]
    else:
        assert SettingsStore(path).read()["starred_thread_keys"] is None


def test_managed_sink_does_not_repeat_preparation_on_the_main_loop(recovery, monkeypatch):
    from blueferry.sinks import sqlite as sqlite_mod

    recovery.wallet.locked = False
    recovery.unlock()
    monkeypatch.setattr(sqlite_mod, "prune_events", lambda **_kwargs: pytest.fail("repeated scan"))
    monkeypatch.setattr(
        sqlite_mod, "minimize_ancs_history", lambda **_kwargs: pytest.fail("repeated scan"),
    )
    SqliteSink(storage=recovery.storage)


def test_disabling_retention_succeeds_when_wallet_key_deletion_is_unavailable(recovery, monkeypatch):
    def locked(**_kwargs):
        raise StorageUnavailableError("wallet locked")

    r = recovery
    monkeypatch.setattr(r.wallet, "delete", locked, raising=False)
    r.operations.change_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)), policy="none",
    )
    r.queue.finish()
    assert r.outcomes[-1]["storage_state"] == "disabled"
    assert "remove the old BlueFerry key" in r.outcomes[-1]["storage_detail"]
    assert SettingsStore(r.storage.settings_path).read()["local_data"] == "none"
    assert read_events() == []


def _seal_history(r) -> bytes:
    """Prepare once with the wallet's key, which records the key check."""
    r.wallet.locked = False
    r.unlock(interactive=True)
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert SettingsStore(r.storage.settings_path).read()[KEY_CHECK_SETTING]
    append_event({"kind": "sms_received", "handle": "original", "body": "keep"}, storage=r.storage)
    return config.EVENTS_DB.read_bytes()


@pytest.mark.parametrize("interactive", [False, True])
def test_lost_keyring_key_is_reported_instead_of_silently_replaced(recovery, interactive):
    r = recovery
    before = _seal_history(r)
    original, r.wallet.key = r.wallet.key, None  # e.g. its wallet is not the one open
    r.storage.require_preparation()  # A restart.

    r.unlock(interactive=interactive)

    assert r.wallet.created == 0
    assert r.storage.status == StorageStatus("encrypted", "locked", KEY_MISSING_DETAIL)
    assert r.outcomes[-1]["storage_detail"] == KEY_MISSING_DETAIL
    assert r.storage.reset_offered
    assert config.EVENTS_DB.read_bytes() == before
    # Unlike corruption, this keeps the passive retry watching for the key.
    r.wallet.key = original
    r.unlock()
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert read_events(storage=r.storage)[0]["handle"] == "original"


def test_replaced_keyring_key_is_recognised_without_touching_saved_data(recovery):
    r = recovery
    before = _seal_history(r)
    original, r.wallet.key = r.wallet.key, b"L" * 32
    r.storage.require_preparation()

    r.unlock(interactive=True)

    assert r.storage.status == StorageStatus("encrypted", "locked", KEY_MISMATCH_DETAIL)
    assert config.EVENTS_DB.read_bytes() == before
    r.wallet.key = original
    r.unlock(interactive=True)
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert read_events(storage=r.storage)[0]["handle"] == "original"


def _event_rows() -> int:
    with closing(sqlite3.connect(config.EVENTS_DB)) as database:
        return database.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_reset_erases_unreadable_storage_and_starts_over(recovery):
    r = recovery
    path = r.storage.settings_path
    thread = "address:phone:15551111111"
    # Sealed by an earlier release, which kept no key check.
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    append_event({"kind": "sms_received", "handle": "old", "body": "lost"}, storage=r.storage)
    ContactRepository(r.storage).replace([("Alice", ["15551111111"], [])])
    StarredThreadsStore(path, storage=r.storage).set_starred(thread, True)
    # That release replaced a missing key on the next unlock (#298).
    r.wallet.key = None
    r.storage.require_preparation()
    r.unlock(interactive=True)
    assert r.storage.status.state == "error"
    assert "open the wallet that holds the original key" in r.storage.status.detail
    assert "reset local storage" in r.storage.status.detail
    operations = BackendOperations(r.daemon.sessions, BackendDependencies(
        storage=r.storage, contacts=r.daemon.contacts,
        starred_threads=r.daemon.starred_threads, group_routes=r.daemon.group_routes,
        confirmed_groups=r.daemon.confirmed_groups,
        prepare_storage=preparation_mod.prepare_storage,
        on_storage_prepared=r.daemon._apply_storage_preparation,
        on_storage_changed=r.daemon._on_storage_changed,
    ))

    def reset(*, confirmed):
        operations.reset_storage_async(
            r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)),
            confirmed=confirmed,
        )

    with pytest.raises(ConfirmationRequiredError):
        reset(confirmed=False)
    assert _event_rows() == 1 and not r.queue.jobs

    reset(confirmed=True)
    assert _event_rows() == 0  # Erased before the old keys go.
    r.queue.finish()

    assert r.outcomes[-1]["storage_state"] == "ready"
    assert r.published[-1].state == "ready"
    assert r.wallet.deleted == 1 and r.wallet.created == 2
    assert read_events(storage=r.storage) == []
    assert r.daemon.contacts.resolve("+15551111111") is None
    assert StarredThreadsStore(path, storage=r.storage).keys() == []
    assert SettingsStore(path).read()[KEY_CHECK_SETTING]
    # New data is sealed under the new key, which a later start verifies.
    append_event({"kind": "sms_received", "handle": "new", "body": "kept"}, storage=r.storage)
    r.storage.require_preparation()
    r.unlock()
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert [event["handle"] for event in read_events(storage=r.storage)] == ["new"]


@pytest.mark.parametrize("database, clear", [
    ("EVENTS_DB", clear_events),
    ("CONTACTS_DB", clear_contact_cache),
    ("CALLS_DB", clear_call_history),
])
def test_reset_replaces_a_database_sqlite_cannot_open(recovery, database, clear):
    r = recovery
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    now = datetime.now(timezone.utc)
    append_event({"kind": "sms_received", "handle": "old", "body": "lost"}, storage=r.storage)
    ContactRepository(r.storage).replace([("Alice", ["15551111111"], [])])
    CallHistoryRepository(r.storage).replace([CallRecord(
        direction=MISSED, occurred_at=now, raw_time=now.strftime("%Y%m%dT%H%M%SZ"),
        address="+15551111111", phone="15551111111", name=None,
    )], now=now)
    path = getattr(config, database)
    damaged = b"not a database" * 512
    path.write_bytes(damaged)
    sidecar = path.with_name(path.name + "-wal")
    sidecar.write_bytes(b"stale log")

    # Ordinary erasing never deletes a damaged file; only a reset does.
    with pytest.raises(sqlite3.DatabaseError):
        clear()
    assert path.read_bytes() == damaged

    r.operations.reset_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)),
        confirmed=True,
    )
    r.queue.finish()

    assert r.outcomes[-1]["storage_state"] == "ready"
    assert not sidecar.exists()
    assert not path.exists() or path.read_bytes() != damaged
    assert read_events(storage=r.storage) == []
    assert ContactRepository(r.storage).load() == []
    assert CallHistoryRepository(r.storage).load(now=now) == []


def test_reset_waits_for_a_contact_sync_that_has_the_cache_open(monkeypatch):
    ContactRepository().replace([("Old", ["15550000000"], [])])
    reset_thread = threading.current_thread()
    opened = threading.Event()
    reset_waiting = threading.Event()
    real_open, real_lock = repository_mod._open_db, repository_mod._STORE_LOCK

    class WatchedLock:
        def __enter__(self):
            if threading.current_thread() is reset_thread:
                reset_waiting.set()
            real_lock.acquire()

        def __exit__(self, *exc_info):
            real_lock.release()

    def open_db():
        connection = real_open()
        if threading.current_thread() is not reset_thread:
            # Connected but not yet writing: the window the reset must respect.
            opened.set()
            reset_waiting.wait(timeout=10)
        return connection

    monkeypatch.setattr(repository_mod, "_STORE_LOCK", WatchedLock())
    monkeypatch.setattr(repository_mod, "_open_db", open_db)
    errors: list[BaseException] = []

    def sync():
        try:
            ContactRepository().replace([("Alice", ["15551111111"], [])])
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=sync)
    worker.start()
    assert opened.wait(timeout=10)
    clear_contact_cache(discard_damaged=True)
    reset_waiting.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    # The reset ran after the sync's write, so nothing it wrote survives.
    assert ContactRepository().load(strict=True) == []
    path = config.CONTACTS_DB
    assert not path.with_name(path.name + "-journal").exists()


def test_damaged_history_offers_the_reset_that_clears_it(recovery):
    r = recovery
    _seal_history(r)
    damaged = b"not a database" * 512
    config.EVENTS_DB.write_bytes(damaged)
    r.storage.require_preparation()  # A restart.

    r.unlock()

    assert r.storage.status == StorageStatus("encrypted", "error", DAMAGED_DATABASE_DETAIL)
    status = r.daemon._status()
    assert status["storage_reset"] is True
    assert status["storage_detail"] == DAMAGED_DATABASE_DETAIL
    assert config.EVENTS_DB.read_bytes() == damaged

    r.operations.reset_storage_async(
        r.queue.submit, r.outcomes.append, lambda error: pytest.fail(str(error)),
        confirmed=True,
    )
    r.queue.finish()

    assert r.outcomes[-1]["storage_state"] == "ready"
    assert r.daemon._status()["storage_reset"] is False
    append_event({"kind": "sms_received", "handle": "new", "body": "kept"}, storage=r.storage)
    assert [event["handle"] for event in read_events(storage=r.storage)] == ["new"]


def test_replacement_key_is_not_trusted_over_encrypted_preferences(recovery):
    r = recovery
    path = r.storage.settings_path
    thread = "address:phone:15551111111"
    # An earlier release kept no key check and, here, no history either.
    r.wallet.locked = False
    r.storage.refresh(allow_prompt=False)
    StarredThreadsStore(path, storage=r.storage).set_starred(thread, True)
    original, r.wallet.key = r.wallet.key, None
    r.storage.require_preparation()

    r.unlock(interactive=True)

    assert r.storage.status.state == "error"
    assert not SettingsStore(path).read().get(KEY_CHECK_SETTING)
    r.wallet.key = original
    r.unlock(interactive=True)
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert StarredThreadsStore(path, storage=r.storage).keys() == [thread]


def _unreadable_settings_daemon(make_daemon, monkeypatch):
    """Start with plaintext history and contacts, and settings.json damaged."""
    wallet = _Wallet()
    wallet.locked = False
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda *_args: 1)
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)
    monkeypatch.setattr(daemon_mod, "ObexWorker", _Queue)
    monkeypatch.setattr(
        daemon_mod, "StorageSecurity",
        lambda **kwargs: StorageSecurity(key_provider=wallet, **kwargs),
    )
    path = config.SETTINGS_JSON
    SettingsStore(path).update(local_data="plaintext")
    seeded = StorageSecurity(key_provider=wallet, initialize=False)
    append_event({"kind": "sms_received", "handle": "kept", "body": "private"}, storage=seeded)
    ContactRepository(seeded).replace([("Alice", ["15551111111"], [])])
    saved = path.read_text(encoding="utf-8")
    path.write_text(saved[:-3], encoding="utf-8")
    daemon = make_daemon()
    operations = BackendOperations(daemon.sessions, BackendDependencies(
        storage=daemon.storage, contacts=daemon.contacts,
        starred_threads=daemon.starred_threads, group_routes=daemon.group_routes,
        confirmed_groups=daemon.confirmed_groups,
        prepare_storage=preparation_mod.prepare_storage,
        on_storage_prepared=daemon._apply_storage_preparation,
        on_storage_changed=daemon._on_storage_changed,
    ))
    queue = _Queue()
    daemon.storage.require_preparation()
    operations.retry_storage_async(
        queue.submit, lambda _status: None, lambda error: pytest.fail(str(error)),
        initialize=True,
    )
    assert not queue.jobs
    assert "settings.json cannot be read" in daemon.storage.status.detail
    return SimpleNamespace(
        daemon=daemon, operations=operations, queue=queue, wallet=wallet, saved=saved,
    )


@pytest.mark.parametrize("change", ["none", "plaintext", "encrypted", "reset"])
def test_storage_changes_are_refused_while_settings_cannot_be_read(
    make_daemon, monkeypatch, change,
):
    r = _unreadable_settings_daemon(make_daemon, monkeypatch)
    before = {
        path: path.read_bytes()
        for path in (config.SETTINGS_JSON, config.EVENTS_DB, config.CONTACTS_DB)
    }
    status = r.daemon.storage.status

    def replied(value):
        pytest.fail(f"unexpected reply: {value}")

    with pytest.raises(ConfigurationError, match=r"settings\.json can't be read") as caught:
        if change == "reset":
            r.operations.reset_storage_async(
                r.queue.submit, replied, replied, confirmed=True,
            )
        else:
            r.operations.change_storage_async(
                r.queue.submit, replied, replied, policy=change,
            )

    assert caught.value.dbus_suffix == "Configuration"
    # Nothing was erased, migrated or rewritten, and the wallet was untouched.
    assert {path: path.read_bytes() for path in before} == before
    assert not r.queue.jobs and r.wallet.deleted == 0 and r.wallet.created == 0
    assert r.daemon.storage.status == status


def test_policy_change_after_settings_reads_again_uses_the_saved_policy(
    make_daemon, monkeypatch,
):
    r = _unreadable_settings_daemon(make_daemon, monkeypatch)
    # Repaired before the retry tick has read it again.
    config.SETTINGS_JSON.write_text(r.saved, encoding="utf-8")
    outcomes = []

    r.operations.change_storage_async(
        r.queue.submit, outcomes.append, lambda error: pytest.fail(str(error)),
        policy="encrypted",
    )
    r.queue.finish()

    # The saved policy was plaintext, so its records were erased, not kept
    # beside encrypted ones.
    assert outcomes[-1]["storage_policy"] == "encrypted"
    assert outcomes[-1]["storage_state"] == "ready"
    assert _event_rows() == 0
    assert SettingsStore(config.SETTINGS_JSON).read()["local_data"] == "encrypted"
