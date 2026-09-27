"""MAP gets a bounded head start without starving usable PBAP contacts."""
import sqlite3
from contextlib import closing
from queue import Queue
from types import SimpleNamespace

import pytest

from blueferry import config, contact_repository
from blueferry import contact_sync as contact_sync_mod
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.contact_sync import ContactSync
from blueferry.contacts import ContactsResolver
from blueferry.obex import worker as worker_mod


class _Storage:
    """Writable storage whose snapshots need no key."""

    revision = 0

    def __init__(self):
        self.status = SimpleNamespace(can_write=True)

    def snapshot(self):
        return SimpleNamespace(close=lambda: None)


@pytest.fixture
def contacts_setup():
    jobs, timers, daily, removed, refreshed, errors = [], {}, [], [], [], []
    cached = [0]
    timer_serial = 0

    def schedule(delay, callback):
        nonlocal timer_serial
        timer_serial += 1
        if delay == contact_sync_mod.CONTACTS_REFRESH_SEC:
            daily.append(callback)
        else:
            timers[timer_serial] = (delay, callback)
        return timer_serial

    def cancel(timer_id):
        removed.append(timer_id)
        del timers[timer_id]

    def grace_timer():
        (timer_id,) = timers
        return timer_id

    def expire(timer_id):
        delay, callback = timers.pop(timer_id)
        assert delay == contact_sync_mod.CONTACTS_MAP_GRACE_SECONDS
        assert callback() is False

    def default_pull():
        cached[0] = 3
        return cached[0]

    def finish():
        operation, handlers = jobs.pop(0)
        handlers['on_success'](operation())

    def fail(error):
        _operation, handlers = jobs.pop(0)
        handlers['on_error'](error)

    r = SimpleNamespace(
        jobs=jobs, timers=timers, daily=daily, removed=removed, refreshed=refreshed,
        errors=errors, cached=cached, grace_timer=grace_timer, expire=expire,
        finish=finish, fail=fail, pull=default_pull, storage=_Storage(),
        sessions=SimpleNamespace(map=None, pbap=object(), report_error=errors.append),
        worker=SimpleNamespace(
            submit=lambda operation, **handlers: jobs.append((operation, handlers)),
        ),
    )

    def build(contacts=None):
        r.contacts = contacts or SimpleNamespace(
            count=lambda: cached[0], refresh=lambda: cached[0],
        )
        r.sync = ContactSync(
            sessions=r.sessions,
            storage=r.storage,
            contacts=r.contacts,
            submit=lambda *args, **kwargs: r.worker.submit(*args, **kwargs),
            on_refreshed=lambda: refreshed.append(True),
            pull=lambda _sessions, *, storage: r.pull(),
            schedule=schedule,
            cancel=cancel,
        )
        r.operations = BackendOperations(
            r.sessions, BackendDependencies(sync_contacts=r.sync.sync),
        )

    r.build = build
    build()
    return r


def test_pbap_only_sync_resumes_at_deadline_and_daily_refresh_does_not_wait_again(contacts_setup):
    r = contacts_setup
    r.sync.profiles_available()
    timer = r.grace_timer()
    for _retry in range(20):
        r.sync.profiles_available()
    r.sync.refresh()
    assert not r.jobs
    assert list(r.timers) == [timer]  # Retries cannot push the deadline back.

    r.expire(timer)
    assert len(r.jobs) == 1
    r.sync.profiles_available()
    assert len(r.jobs) == 1
    r.finish()
    assert r.cached[0] == 3
    assert r.refreshed == [True]
    assert r.sessions.map is None

    assert len(r.daily) == 1  # One daily timer despite repeated setup.
    assert r.daily[0]() is True
    assert len(r.jobs) == 1
    assert not r.timers
    r.finish()


@pytest.mark.parametrize('initial_sync', ['automatic', 'manual'])
def test_successful_zero_contact_sync_is_not_repeated_on_recovery(contacts_setup, initial_sync):
    r = contacts_setup
    r.pull = lambda: 0
    r.sync.profiles_available()
    completed, failures = [], []
    if initial_sync == 'automatic':
        r.expire(r.grace_timer())
    else:
        r.operations.sync_contacts(completed.append, failures.append)
    r.finish()
    assert r.cached[0] == 0
    assert r.refreshed == [True]

    for _retry in range(5):
        r.sync.profiles_available()
        r.sync.storage_changed()
    r.sessions.map = object()
    r.sync.profiles_available()
    assert not r.jobs
    assert not r.timers

    r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    r.finish()
    assert completed == ([0, 0] if initial_sync == 'manual' else [0])
    assert not failures
    r.sync.refresh()
    assert len(r.jobs) == 1
    r.finish()
    r.sync.profiles_available()
    assert not r.jobs


def test_failed_initial_contact_sync_can_retry_on_profile_recovery(contacts_setup):
    r = contacts_setup
    r.sync.profiles_available()
    r.expire(r.grace_timer())
    r.fail(RuntimeError('temporary download failure'))
    assert not r.jobs
    r.sync.profiles_available()
    assert len(r.jobs) == 1
    r.finish()
    r.sync.profiles_available()
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
    r.build(contacts=ContactsResolver())
    previous_addresses = r.contacts.thread_addresses('15555550111')
    replacement = [('Alice', ['15555550222'], ['alice@example.com'])]
    r.pull = lambda: repository.replace(replacement)

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
    assert not r.sync.initial_sync_done
    assert not r.sync.pending
    assert not r.sync.waiting
    assert r.contacts.records() == previous
    assert r.contacts.thread_addresses('15555550111') == previous_addresses
    assert r.contacts.count() == (2 if previously_cached else 0)
    assert repository.load(strict=True) == replacement

    if previously_cached:
        r.operations.sync_contacts(completed.append, failed.append)
    else:
        r.sync.profiles_available()
    assert len(r.jobs) == 1
    r.finish()
    assert r.contacts.records() == replacement
    assert not r.contacts.thread_addresses('15555550111')
    assert r.sync.initial_sync_done
    r.sync.profiles_available()
    assert not r.jobs


def test_map_connecting_early_cancels_the_wait_and_starts_one_pull(contacts_setup):
    r = contacts_setup
    r.sync.refresh()
    timer = r.grace_timer()
    r.sessions.map = object()
    r.sync.profiles_available()
    assert len(r.jobs) == 1
    assert r.removed == [timer]
    assert not r.timers
    r.sync.profiles_available()
    assert len(r.jobs) == 1
    r.finish()

    r.sessions.map = None
    r.sync.refresh()
    assert len(r.jobs) == 1
    assert not r.timers


def test_manual_sync_satisfies_the_deferred_automatic_pull(contacts_setup):
    r = contacts_setup
    r.sync.refresh()
    timer = r.grace_timer()
    completed = []
    r.operations.sync_contacts(completed.append, lambda error: pytest.fail(str(error)))
    r.finish()
    assert completed == [3]
    assert r.removed == [timer]
    assert not r.sync.deferred
    r.sync.profiles_available()
    assert not r.jobs
    assert not r.timers


def test_manual_sync_spanning_deadline_prevents_a_second_download(contacts_setup):
    r = contacts_setup
    r.sync.refresh()
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    r.expire(r.grace_timer())
    r.sync.refresh()
    assert len(r.jobs) == 1
    r.finish()
    assert completed == [3]
    assert not failures
    assert not r.jobs
    assert not r.sync.pending
    assert not r.sync.deferred


def test_manual_callers_join_an_automatic_download(contacts_setup):
    r = contacts_setup
    r.sync.refresh()
    r.expire(r.grace_timer())
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    r.finish()
    assert completed == [3, 3]
    assert r.refreshed == [True]
    assert not failures
    assert not r.sync.waiting


def test_failed_manual_sync_after_deadline_leaves_one_automatic_attempt(contacts_setup):
    r = contacts_setup
    r.sync.refresh()
    completed, failures = [], []
    for _caller in range(2):
        r.operations.sync_contacts(completed.append, failures.append)
    r.expire(r.grace_timer())
    error = RuntimeError('phonebook download failed')
    r.fail(error)
    assert len(failures) == 2
    assert not completed
    assert r.errors == [error]  # One failed transfer, one transport report.
    assert len(r.jobs) == 1
    assert not r.sync.deferred
    assert not r.sync.waiting

    r.fail(error)
    assert not r.jobs  # The automatic fallback must not become a retry loop.
    assert not r.sync.pending
    r.operations.sync_contacts(completed.append, failures.append)
    r.finish()
    assert completed == [3]


def test_queue_failure_releases_manual_waiters_and_allows_retry(contacts_setup):
    r = contacts_setup
    submit = r.worker.submit
    error = RuntimeError('OBEX operation queue is full')
    def reject(*_args, **_kwargs):
        raise error
    r.worker.submit = reject
    completed, failures = [], []
    r.operations.sync_contacts(completed.append, failures.append)
    assert len(failures) == 1
    assert r.errors == [error]
    assert not r.sync.pending
    assert not r.sync.waiting
    r.worker.submit = submit
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
    r.contacts.refresh = reject
    r.finish()
    assert not completed
    assert len(failures) == 2
    assert r.errors == [error]
    assert not r.sync.pending
    assert not r.sync.waiting


def test_a_broken_client_reply_cannot_strand_other_waiters(contacts_setup):
    r = contacts_setup
    def broken_reply(_result):
        raise RuntimeError('client disappeared')
    r.operations.sync_contacts(broken_reply, broken_reply)
    completed = []
    r.operations.sync_contacts(completed.append, broken_reply)
    r.finish()
    assert completed == [3]
    assert not r.sync.pending
    assert not r.sync.waiting
    assert not r.errors  # Reply delivery is not a Bluetooth failure.


def test_coalesced_manual_requests_remain_bounded(contacts_setup, monkeypatch):
    r = contacts_setup
    monkeypatch.setattr(contact_sync_mod, 'MAX_OBEX_PENDING_OPERATIONS', 2)
    completed, failures = [], []
    for _caller in range(3):
        r.operations.sync_contacts(completed.append, failures.append)
    assert len(r.jobs) == 1
    assert r.sync.waiting == 2
    assert len(failures) == 1
    assert not r.errors
    r.finish()
    assert completed == [3, 3]


def test_coalesced_sync_and_power_recovery_share_the_worker_safely(contacts_setup, monkeypatch):
    r = contacts_setup
    callbacks = Queue()
    monkeypatch.setattr(worker_mod, 'initialize_obex_worker_bus', lambda: None)
    monkeypatch.setattr(worker_mod, 'close_obex_worker_bus', lambda: None)
    monkeypatch.setattr(worker_mod.GLib, 'idle_add', lambda fn, *args: callbacks.put((fn, args)))
    worker = worker_mod.ObexWorker()
    r.worker = worker
    completed, failed = [], []
    try:
        r.operations.sync_contacts(completed.append, failed.append)
        callback, args = callbacks.get(timeout=2)
        r.operations.sync_contacts(completed.append, failed.append)
        assert not worker.reserve_if_idle()  # The sync's replies are still pending.
        callback(*args)
        assert completed == [3, 3]
        assert worker.reserve_if_idle()
        r.operations.sync_contacts(completed.append, failed.append)
        assert len(failed) == 1 and 'recovery' in str(failed[0])
        assert not r.sync.pending
        assert not r.sync.waiting
        worker.release()
        r.operations.sync_contacts(completed.append, failed.append)
        callback, args = callbacks.get(timeout=2)
        callback(*args)
        assert completed == [3, 3, 3]
        assert callbacks.empty()
    finally:
        worker.shutdown()


@pytest.mark.parametrize('unavailable', ['storage', 'pbap'])
@pytest.mark.parametrize('cached', [0, 5])
def test_recovery_after_expired_wait_preserves_refresh_without_rearming(
    contacts_setup, unavailable, cached,
):
    r = contacts_setup
    r.cached[0] = cached
    r.sync.refresh()
    if unavailable == 'storage':
        r.storage.status.can_write = False
    else:
        r.sessions.pbap = None
    r.expire(r.grace_timer())
    assert not r.jobs
    assert r.sync.deferred

    if unavailable == 'storage':
        r.storage.status.can_write = True
        r.sync.storage_changed()
    else:
        r.sessions.pbap = object()
        r.sync.profiles_available()
    assert len(r.jobs) == 1
    assert not r.timers
    r.finish()
