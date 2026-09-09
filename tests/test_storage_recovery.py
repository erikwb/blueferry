"""Late keyring availability must recover without a client or password prompt."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from gi.repository import GLib

from blueferry import config
from blueferry import daemon as daemon_mod
from blueferry import history as history_mod
from blueferry import storage_preparation as preparation_mod
from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.contact_repository import ContactRepository
from blueferry.contacts import ContactsResolver
from blueferry.events import SmsEvent
from blueferry.history import append_event, read_events
from blueferry.settings_store import SettingsStore
from blueferry.sinks.sqlite import SqliteSink
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_security import StorageSecurity, StorageStatus, StorageUnavailableError


class _Wallet:
    locked = True
    key = b"K" * 32

    def get_or_create(self, *, allow_prompt, cancellable=None):
        if self.locked and not allow_prompt:
            raise StorageUnavailableError("the desktop keyring is locked")
        return self.key


class _Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, operation, **handlers):
        self.jobs.append((operation, handlers))

    def finish(self):
        operation, handlers = self.jobs.pop(0)
        try:
            value = operation()
        except Exception as error:
            handlers["on_error"](error)
        else:
            handlers["on_success"](value)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 30)
    monkeypatch.setattr(GLib, "timeout_add_seconds", lambda *_args: 1)
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)
    wallet = _Wallet()
    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=wallet,
    )
    daemon = object.__new__(daemon_mod.Daemon)
    daemon.storage = storage
    daemon.contacts = ContactsResolver(storage=storage)
    daemon.starred_threads = SimpleNamespace(migrate=lambda: None)
    daemon.confirmed_groups = SimpleNamespace(migrate=lambda: None)
    seeded = []
    daemon.events = SimpleNamespace(seed_historical_ancs=seeded.extend)
    daemon._mark_setup_task = lambda _task: None
    published = []
    daemon._emit_status = lambda: published.append(storage.status)
    daemon._dbus_service = None
    daemon._contacts_refresh_id = None
    daemon._contacts_refresh_pending = False
    daemon.listener = None
    daemon.sessions = SimpleNamespace(pbap=object(), map=None, report_error=lambda _error: None)
    daemon.obex_worker = _Queue()
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

    yield SimpleNamespace(
        storage=storage, wallet=wallet, daemon=daemon, operations=operations,
        queue=queue, outcomes=outcomes, published=published, seeded=seeded, unlock=unlock,
    )
    storage.close()


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

        def get_or_create(self, *, allow_prompt, cancellable=None):
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
    assert not r.seeded

    r.wallet.locked = False
    r.unlock(interactive=interactive)

    retained = read_events(storage=r.storage)
    assert len(retained) == 2
    assert retained[0]["handle"] == "recent"
    assert "extra_private_field" not in retained[1]
    assert r.seeded == [retained[1]]
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
    if cached:
        r.wallet.locked = False
        r.storage.refresh(allow_prompt=False)
        ContactRepository(r.storage).replace([("Alice", ["15551111111"], [])])
        r.wallet.locked = True
    r.storage.refresh(allow_prompt=False)
    r.daemon.contacts.refresh()
    r.daemon._post_available_sessions_setup()
    assert not r.daemon.obex_worker.jobs  # Defer pulls while persistence is unavailable.
    assert r.daemon._contacts_refresh_id is not None

    def pull(_sessions, *, storage):
        assert storage.status.can_write
        return ContactRepository(storage).replace([("Alice", ["15551111111"], [])])

    monkeypatch.setattr(daemon_mod, "pull_phonebook", pull)
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
    submit = r.daemon.obex_worker.submit

    def full(*_args, **_kwargs):
        raise RuntimeError("OBEX operation queue is full")

    monkeypatch.setattr(r.daemon.obex_worker, "submit", full)
    r.wallet.locked = False
    r.unlock()
    assert r.outcomes[-1]["storage_state"] == "ready"
    assert r.published[-1].state == "ready"
    assert not r.daemon._contacts_refresh_pending
    monkeypatch.setattr(r.daemon.obex_worker, "submit", submit)
    r.daemon._refresh_contacts()
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
