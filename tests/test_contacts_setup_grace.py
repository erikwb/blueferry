"""MAP gets a bounded head start without starving usable PBAP contacts."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from blueferry import config, contact_repository
from blueferry import daemon as daemon_mod
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.contacts import ContactsResolver


@pytest.fixture
def contacts_setup(monkeypatch):
    jobs, timers, removed, verified, errors = [], {}, [], [], []
    cached = [0]
    timer_serial = 0

    def schedule(delay, callback):
        nonlocal timer_serial
        timer_serial += 1
        timers[timer_serial] = (delay, callback)
        return timer_serial

    def cancel(timer_id):
        removed.append(timer_id)
        del timers[timer_id]

    def expire(timer_id):
        delay, callback = timers.pop(timer_id)
        assert delay == daemon_mod.CONTACTS_MAP_GRACE_SECONDS
        assert callback() is False

    def pull():
        cached[0] = 3
        return cached[0]

    def finish():
        operation, handlers = jobs.pop(0)
        handlers['on_success'](operation())

    def fail(error):
        _operation, handlers = jobs.pop(0)
        handlers['on_error'](error)

    value = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    value.sessions = SimpleNamespace(map=None, pbap=object(), report_error=errors.append)
    value.storage = SimpleNamespace(status=SimpleNamespace(can_write=True))
    value.contacts = SimpleNamespace(count=lambda: cached[0], refresh=lambda: cached[0])
    value._contacts_refresh_id = 99
    value._contacts_refresh_pending = False
    value._contacts_initial_sync_done = False
    value._contacts_storage_generation = 0
    value._contacts_sync_waiters = []
    value._contacts_refresh_deferred = False
    value._contacts_map_wait_id = None
    value._contacts_map_wait_finished = False
    value._dbus_service = None
    value.listener = object()
    value._pull_contacts = pull
    value._mark_setup_task = verified.append
    value._emit_status = lambda: None
    value.obex_worker = SimpleNamespace(
        submit=lambda operation, **handlers: jobs.append((operation, handlers)),
    )
    monkeypatch.setattr(daemon_mod.GLib, 'timeout_add_seconds', schedule)
    monkeypatch.setattr(daemon_mod.GLib, 'source_remove', cancel)
    operations = BackendOperations(value.sessions, BackendDependencies(sync_contacts=value._sync_contacts))
    return SimpleNamespace(
        daemon=value, jobs=jobs, timers=timers, removed=removed,
        expire=expire, finish=finish, cached=cached, verified=verified,
        fail=fail, errors=errors, operations=operations,
    )


def test_pbap_only_sync_resumes_at_deadline_and_daily_refresh_does_not_wait_again(contacts_setup):
    r = contacts_setup
    r.daemon._post_available_sessions_setup()
    timer = r.daemon._contacts_map_wait_id
    for _retry in range(20):
        r.daemon._post_available_sessions_setup()
    r.daemon._periodic_refresh_contacts()
    assert not r.jobs
    assert list(r.timers) == [timer]  # Retries cannot push the deadline back.

    r.expire(timer)
    assert len(r.jobs) == 1
    r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    r.finish()
    assert r.cached[0] == 3
    assert r.verified == [daemon_mod.CONTACTS]
    assert r.daemon.sessions.map is None

    r.daemon._periodic_refresh_contacts()
    assert len(r.jobs) == 1
    assert not r.timers
    r.finish()


@pytest.mark.parametrize('initial_sync', ['automatic', 'manual'])
def test_successful_zero_contact_sync_is_not_repeated_on_recovery(contacts_setup, initial_sync):
    r = contacts_setup
    r.daemon._pull_contacts = lambda: 0
    r.daemon._post_available_sessions_setup()
    completed, failures = [], []
    if initial_sync == 'automatic':
        r.expire(r.daemon._contacts_map_wait_id)
    else:
        r.operations.sync_contacts(completed.append, failures.append)
    r.finish()
    assert r.cached[0] == 0
    assert r.verified == [daemon_mod.CONTACTS]

    for _retry in range(5):
        r.daemon._post_available_sessions_setup()
        r.daemon._on_storage_changed()
    r.daemon.sessions.map = object()
    r.daemon._post_available_sessions_setup()
    assert not r.jobs
    assert not r.timers

    r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    r.finish()
    assert completed == ([0, 0] if initial_sync == 'manual' else [0])
    assert not failures
    r.daemon._periodic_refresh_contacts()
    assert len(r.jobs) == 1
    r.finish()
    r.daemon._post_available_sessions_setup()
    assert not r.jobs


def test_failed_initial_contact_sync_can_retry_on_profile_recovery(contacts_setup):
    r = contacts_setup
    r.daemon._post_available_sessions_setup()
    r.expire(r.daemon._contacts_map_wait_id)
    r.fail(RuntimeError('temporary download failure'))
    assert not r.jobs
    r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    r.finish()
    r.daemon._post_available_sessions_setup()
    assert not r.jobs


@pytest.mark.parametrize('previously_cached', [False, True])
def test_locked_cache_reload_preserves_contacts_and_does_not_complete_initial_sync(
    contacts_setup, tmp_path, monkeypatch, previously_cached,
):
    r = contacts_setup
    monkeypatch.setattr(config, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONTACTS_DB', tmp_path / 'contacts.sqlite')
    # Exercise a real SQLite lock without spending its default five-second
    # busy timeout on each failure.
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda path: connect(path, timeout=0))
    repository = contact_repository.ContactRepository()
    previous = [('Bob', ['15555550111'], ['bob@example.com'])] if previously_cached else []
    repository.replace(previous)
    r.daemon.contacts = ContactsResolver()
    previous_addresses = r.daemon.contacts.thread_addresses('15555550111')
    replacement = [('Alice', ['15555550222'], ['alice@example.com'])]
    r.daemon._pull_contacts = lambda: repository.replace(replacement)

    completed, failed = [], []
    r.operations.sync_contacts(completed.append, failed.append)
    operation, handlers = r.jobs.pop(0)
    pulled = operation()
    with closing(sqlite3.connect(config.CONTACTS_DB)) as locker:
        locker.execute('BEGIN EXCLUSIVE')
        try:
            handlers['on_success'](pulled)
        finally:
            locker.rollback()

    assert not completed
    assert len(failed) == 1 and 'database is locked' in str(failed[0])
    assert len(r.errors) == 1 and isinstance(r.errors[0], sqlite3.OperationalError)
    assert not r.daemon._contacts_initial_sync_done
    assert not r.daemon._contacts_refresh_pending
    assert not r.daemon._contacts_sync_waiters
    assert r.daemon.contacts.records() == previous
    assert r.daemon.contacts.thread_addresses('15555550111') == previous_addresses
    assert r.daemon.contacts.count() == (2 if previously_cached else 0)
    assert repository.load(strict=True) == replacement

    if previously_cached:
        r.operations.sync_contacts(completed.append, failed.append)
    else:
        r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    r.finish()
    assert r.daemon.contacts.records() == replacement
    assert not r.daemon.contacts.thread_addresses('15555550111')
    assert r.daemon._contacts_initial_sync_done
    r.daemon._post_available_sessions_setup()
    assert not r.jobs


def test_map_connecting_early_cancels_the_wait_and_starts_one_pull(contacts_setup):
    r = contacts_setup
    r.daemon._refresh_contacts()
    timer = r.daemon._contacts_map_wait_id
    r.daemon.sessions.map = object()
    r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    assert r.removed == [timer]
    assert not r.timers
    r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    r.finish()

    r.daemon.sessions.map = None
    r.daemon._periodic_refresh_contacts()
    assert len(r.jobs) == 1
    assert not r.timers


def test_manual_sync_satisfies_the_deferred_automatic_pull(contacts_setup):
    r = contacts_setup
    r.daemon._refresh_contacts()
    timer = r.daemon._contacts_map_wait_id
    completed = []
    r.operations.sync_contacts(completed.append, lambda error: pytest.fail(str(error)))
    r.finish()
    assert completed == [3]
    assert r.removed == [timer]
    assert not r.daemon._contacts_refresh_deferred
    r.daemon._post_available_sessions_setup()
    assert not r.jobs
    assert not r.timers


def test_manual_sync_spanning_deadline_prevents_a_second_download(contacts_setup):
    r = contacts_setup
    r.daemon._refresh_contacts()
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    r.expire(r.daemon._contacts_map_wait_id)
    r.daemon._periodic_refresh_contacts()
    assert len(r.jobs) == 1
    r.finish()
    assert completed == [3]
    assert not failures
    assert not r.jobs
    assert not r.daemon._contacts_refresh_pending
    assert not r.daemon._contacts_refresh_deferred


def test_manual_callers_join_an_automatic_download(contacts_setup):
    r = contacts_setup
    r.daemon._refresh_contacts()
    r.expire(r.daemon._contacts_map_wait_id)
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    r.finish()
    assert completed == [3, 3]
    assert r.verified == [daemon_mod.CONTACTS]
    assert not failures
    assert not r.daemon._contacts_sync_waiters


def test_failed_manual_sync_after_deadline_leaves_one_automatic_attempt(contacts_setup):
    r = contacts_setup
    r.daemon._refresh_contacts()
    completed, failures = [], []
    for _caller in range(2):
        r.operations.sync_contacts(completed.append, failures.append)
    r.expire(r.daemon._contacts_map_wait_id)
    error = RuntimeError('phonebook download failed')
    r.fail(error)
    assert len(failures) == 2
    assert not completed
    assert r.errors == [error]  # One failed transfer, one transport report.
    assert len(r.jobs) == 1
    assert not r.daemon._contacts_refresh_deferred
    assert not r.daemon._contacts_sync_waiters

    r.fail(error)
    assert not r.jobs  # The automatic fallback must not become a retry loop.
    assert not r.daemon._contacts_refresh_pending
    r.operations.sync_contacts(completed.append, failures.append)
    r.finish()
    assert completed == [3]


def test_queue_failure_releases_manual_waiters_and_allows_retry(contacts_setup):
    r = contacts_setup
    submit = r.daemon.obex_worker.submit
    error = RuntimeError('OBEX operation queue is full')
    def reject(*_args, **_kwargs):
        raise error
    r.daemon.obex_worker.submit = reject
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    assert len(failures) == 1
    assert r.errors == [error]
    assert not r.daemon._contacts_refresh_pending
    assert not r.daemon._contacts_sync_waiters
    r.daemon.obex_worker.submit = submit
    r.operations.sync_contacts(completed.append, failures.append)
    r.finish()
    assert completed == [3]


def test_cache_refresh_failure_releases_every_waiter(contacts_setup):
    r = contacts_setup
    completed, failures = [], []
    for _caller in range(2):
        r.operations.sync_contacts(completed.append, failures.append)
    error = RuntimeError('contact cache could not refresh')
    def reject():
        raise error
    r.daemon.contacts.refresh = reject
    r.finish()
    assert not completed
    assert len(failures) == 2
    assert r.errors == [error]
    assert not r.daemon._contacts_refresh_pending
    assert not r.daemon._contacts_sync_waiters


def test_a_broken_client_reply_cannot_strand_other_waiters(contacts_setup):
    r = contacts_setup
    def broken_reply(_result):
        raise RuntimeError('client disappeared')
    r.operations.sync_contacts(broken_reply, broken_reply)
    completed = []
    r.operations.sync_contacts(completed.append, broken_reply)
    r.finish()
    assert completed == [3]
    assert not r.daemon._contacts_refresh_pending
    assert not r.daemon._contacts_sync_waiters
    assert not r.errors  # Reply delivery is not a Bluetooth failure.


def test_coalesced_manual_requests_remain_bounded(contacts_setup, monkeypatch):
    r = contacts_setup
    monkeypatch.setattr(daemon_mod, 'MAX_OBEX_PENDING_OPERATIONS', 2)
    completed, failures = [], []
    for _caller in range(3):
        r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    assert len(r.daemon._contacts_sync_waiters) == 2
    assert len(failures) == 1
    assert not r.errors
    r.finish()
    assert completed == [3, 3]


@pytest.mark.parametrize('unavailable', ['storage', 'pbap'])
@pytest.mark.parametrize('cached', [0, 5])
def test_recovery_after_expired_wait_preserves_refresh_without_rearming(
    contacts_setup, unavailable, cached,
):
    r = contacts_setup
    r.cached[0] = cached
    r.daemon._refresh_contacts()
    if unavailable == 'storage':
        r.daemon.storage.status.can_write = False
    else:
        r.daemon.sessions.pbap = None
    r.expire(r.daemon._contacts_map_wait_id)
    assert not r.jobs
    assert r.daemon._contacts_refresh_deferred

    if unavailable == 'storage':
        r.daemon.storage.status.can_write = True
        r.daemon._on_storage_changed()
    else:
        r.daemon.sessions.pbap = object()
        r.daemon._post_available_sessions_setup()
    assert len(r.jobs) == 1
    assert not r.timers
    r.finish()
