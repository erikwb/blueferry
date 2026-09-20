"""MAP gets a bounded head start without starving usable PBAP contacts."""
from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.backend_operations import BackendDependencies, BackendOperations


@pytest.fixture
def contacts_setup(monkeypatch):
    jobs, timers, removed, verified = [], {}, [], []
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

    value = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    value.sessions = SimpleNamespace(map=None, pbap=object())
    value.storage = SimpleNamespace(status=SimpleNamespace(can_write=True))
    value.contacts = SimpleNamespace(count=lambda: cached[0], refresh=lambda: cached[0])
    value._contacts_refresh_id = 99
    value._contacts_refresh_pending = False
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
    return SimpleNamespace(
        daemon=value, jobs=jobs, timers=timers, removed=removed,
        expire=expire, finish=finish, cached=cached, verified=verified,
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
    operations = BackendOperations(r.daemon.sessions, BackendDependencies(
        submit_obex=r.daemon.obex_worker.submit,
        pull_contacts=r.daemon._pull_contacts,
        on_contacts_pulled=r.daemon._contacts_pulled,
    ))
    operations.sync_contacts(completed.append, lambda error: pytest.fail(str(error)))
    r.finish()
    assert completed == [3]
    assert r.removed == [timer]
    assert not r.daemon._contacts_refresh_deferred
    r.daemon._post_available_sessions_setup()
    assert not r.jobs
    assert not r.timers


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
