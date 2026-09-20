"""MAP gets a bounded head start without starving usable PBAP contacts."""
from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.backend_operations import BackendDependencies, BackendOperations


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
