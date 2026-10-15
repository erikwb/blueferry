"""The saved contact-photo opt-in and switching it at runtime."""
from __future__ import annotations

import itertools
import json
import os

import dbus
import dbus.service
import pytest

from blueferry import config
from blueferry.backend_operations import BackendDependencies
from blueferry.contact_photo_settings import ContactPhotoSettings
from blueferry.dbus_service import MessagesService
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE
from tests.test_calls_dbus import _call, _Sessions
from tests.test_contact_photos_api import _photo_rows, _seed_photo

_service_ids = itertools.count()


def test_local_env_seeds_and_a_saved_choice_wins(tmp_path, monkeypatch, caplog) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    assert ContactPhotoSettings(path).enabled is True

    ContactPhotoSettings(path).set(False)
    assert json.loads(path.read_text())["contact_photos"] is False

    monkeypatch.setenv("BLUEFERRY_CONTACT_PHOTOS", "true")
    caplog.set_level("INFO", logger="blueferry.contact_photo_settings")
    assert ContactPhotoSettings(path, log_overrides=True).enabled is False
    assert "BLUEFERRY_CONTACT_PHOTOS is ignored" in caplog.text


def test_default_is_off_and_garbage_is_ignored(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    assert ContactPhotoSettings(path).enabled is False
    path.write_text('{"contact_photos": 1}')
    path.chmod(0o600)
    assert ContactPhotoSettings(path).enabled is False
    with pytest.raises(ValueError):
        ContactPhotoSettings(path).set(1)  # type: ignore[arg-type]


def test_the_cache_and_sync_follow_the_injected_setting(monkeypatch) -> None:
    from blueferry.contact_sync import ContactSync
    from blueferry.contacts import ContactsResolver

    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    enabled = [True]
    asked = []

    def photos() -> bool:
        asked.append(True)
        return enabled[0]

    resolver = ContactsResolver(photos=photos)
    assert asked  # the cache asked the injected setting, not the seed
    pulls = []

    class _Storage:
        revision = 0
        status = type("S", (), {"can_write": True})()

        def snapshot(self, *, follow):
            return type("Snap", (), {"close": lambda self: None})()

    sync = ContactSync(
        sessions=type("Sessions", (), {"pbap": object(), "map": object()})(),
        storage=_Storage(), contacts=resolver,
        submit=lambda job, *, on_success, on_error: job(),
        on_refreshed=lambda: None,
        pull=lambda _sessions, **kwargs: pulls.append(kwargs) or 0,
        schedule=lambda *_args: 1, cancel=lambda _source: None,
        photos=photos,
    )
    sync.sync()
    enabled[0] = False
    sync._pending = False
    sync.sync()
    assert [call["photos"] for call in pulls] == [True, False]


def test_daemon_switches_photos_on_and_off_without_a_restart(make_daemon, monkeypatch) -> None:
    _seed_photo()
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    daemon = make_daemon()
    assert _photo_rows() == 1
    monkeypatch.setattr(daemon, "_controller_identity", lambda: {})
    statuses = []
    monkeypatch.setattr(daemon, "_emit_status", lambda: statuses.append(True))
    syncs = []
    monkeypatch.setattr(daemon.contact_sync, "refresh", lambda: syncs.append(True))
    jobs = []
    monkeypatch.setattr(
        daemon.obex_worker, "submit",
        lambda job, *, on_success=None, on_error=None: jobs.append((job, on_success)),
    )
    files = []
    daemon.photo_files.clear = lambda: files.append("clear")

    assert daemon._set_contact_photos(False) == {"contact_photos": False}
    assert json.loads(config.SETTINGS_JSON.read_text())["contact_photos"] is False
    assert daemon.photo_files is None and files == ["clear"]
    assert daemon._status()["contact_photos"] is False
    assert daemon._status()["saved_opt_ins"] is True
    # Erasing runs on the OBEX worker, after any pull already queued there.
    [(job, done)] = jobs
    assert _photo_rows() == 1
    done(job())
    assert _photo_rows() == 0

    status = daemon._set_contact_photos(True)
    assert status["contact_photos"] is True
    assert daemon.photo_files is not None
    assert syncs == [True]
    assert len(statuses) >= 3


@pytest.fixture
def queued_photos(make_daemon, monkeypatch):
    _seed_photo()
    monkeypatch.setattr(config, "CONTACT_PHOTOS", True)
    daemon = make_daemon()
    monkeypatch.setattr(daemon, "_emit_status", lambda: None)
    jobs = []
    monkeypatch.setattr(
        daemon.obex_worker, "submit",
        lambda job, **handlers: jobs.append((job, handlers)),
    )
    return daemon, jobs


def _finish_photo_job(jobs):
    job, handlers = jobs.pop(0)
    handlers["on_success"](job())


@pytest.mark.parametrize("retry_by_request", [False, True])
def test_photo_erasure_retries_a_rejected_worker_queue(queued_photos, monkeypatch, retry_by_request):
    daemon, jobs = queued_photos
    submit = daemon.obex_worker.submit

    def refused(*_args, **_kwargs):
        raise RuntimeError("Bluetooth recovery is in progress")

    monkeypatch.setattr(daemon.obex_worker, "submit", refused)
    assert daemon._set_contact_photos(False) == {"contact_photos": False}
    assert _photo_rows() == 1
    assert daemon.photo_files is None
    assert daemon._photo_erase_pending and not daemon._photo_erase_running

    monkeypatch.setattr(daemon.obex_worker, "submit", submit)
    if retry_by_request:
        daemon._set_contact_photos(False)
    else:
        daemon._retry_storage()
    _finish_photo_job(jobs)
    assert _photo_rows() == 0
    assert not daemon._photo_erase_pending


def test_photo_erasure_retries_a_database_failure(queued_photos, monkeypatch):
    from blueferry.contact_repository import ContactRepository

    daemon, jobs = queued_photos
    clear = ContactRepository.clear_photos

    def locked(_self):
        raise OSError("database temporarily unavailable")

    monkeypatch.setattr(ContactRepository, "clear_photos", locked)
    daemon._set_contact_photos(False)
    _finish_photo_job(jobs)
    assert daemon._photo_erase_pending
    assert _photo_rows() == 1

    monkeypatch.setattr(ContactRepository, "clear_photos", clear)
    daemon._retry_storage()
    _finish_photo_job(jobs)
    assert _photo_rows() == 0
    assert not daemon._photo_erase_pending


def test_reenabling_photos_waits_for_pending_erasure(queued_photos, monkeypatch):
    daemon, jobs = queued_photos
    syncs = []
    monkeypatch.setattr(daemon.contact_sync, "photos_changed", lambda: syncs.append(True))
    daemon._set_contact_photos(False)
    daemon._set_contact_photos(True)
    assert syncs == []
    _finish_photo_job(jobs)
    assert syncs == [True]
    assert not daemon._photo_erase_pending


def test_old_erasure_cannot_satisfy_a_later_opt_out(queued_photos):
    daemon, jobs = queued_photos
    daemon._set_contact_photos(False)
    daemon._set_contact_photos(True)
    daemon._set_contact_photos(False)
    _finish_photo_job(jobs)
    # A photo-enabled pull queued between opt-outs could have written after
    # the first erasure. The later opt-out must still erase those bytes.
    _seed_photo()
    assert daemon._photo_erase_pending
    assert len(jobs) == 1
    _finish_photo_job(jobs)
    assert _photo_rows() == 0
    assert not daemon._photo_erase_pending


def test_shutdown_finishes_a_queued_photo_erasure(queued_photos, monkeypatch):
    daemon, _jobs = queued_photos
    monkeypatch.setattr(daemon.sessions, "close_all", lambda **_kwargs: None)
    daemon._set_contact_photos(False)
    assert _photo_rows() == 1
    daemon._cleanup_bluetooth_worker()
    assert _photo_rows() == 0


def test_enabling_photos_during_an_old_pull_starts_a_new_one():
    from types import SimpleNamespace

    from blueferry.contact_sync import ContactSync
    from tests.test_contacts_setup_grace import _Storage

    enabled = [False]
    pulls, jobs = [], []
    sync = ContactSync(
        sessions=SimpleNamespace(map=object(), pbap=object()),
        storage=_Storage(),
        contacts=SimpleNamespace(count=lambda: 3, refresh=lambda: 3),
        submit=lambda job, **handlers: jobs.append((job, handlers)),
        on_refreshed=lambda: None,
        pull=lambda _sessions, **kwargs: pulls.append(kwargs["photos"]) or 3,
        photos=lambda: enabled[0],
        schedule=lambda *_args: 1, cancel=lambda _source: None,
    )
    sync.sync()
    enabled[0] = True
    sync.photos_changed()
    assert len(jobs) == 1
    _finish_photo_job(jobs)
    assert pulls == [False]
    assert len(jobs) == 1 and sync.photo_refresh_pending
    _finish_photo_job(jobs)
    assert pulls == [False, True]
    assert not jobs and not sync.photo_refresh_pending


def test_enabling_photos_offline_preserves_the_refresh_request(queued_photos, monkeypatch):
    daemon, _jobs = queued_photos
    monkeypatch.setattr(daemon.contact_sync, "_initial_sync_done", True)
    monkeypatch.setattr(daemon.contacts, "count", lambda: 3)
    daemon.photo_settings.set(False)
    daemon._set_contact_photos(True)
    assert daemon.contact_sync.needed()
    assert daemon.contact_sync.photo_refresh_pending


def _service(**dependencies):
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Photosp{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(status_provider=lambda: {"initializing": False}, **dependencies),
    )
    return bus, name, service


@pytest.mark.private_dbus
def test_set_contact_photos_round_trips_over_the_bus() -> None:
    toggles = []

    def configure(enabled):
        toggles.append(enabled)
        return {"contact_photos": enabled}

    bus, name, service = _service(set_contact_photos=configure)
    try:
        enabled = json.loads(_call(name, MESSAGES_IFACE, "SetContactPhotos", True)["value"])
        missing = _call(name, MESSAGES_IFACE, "SetContactPhotos", False)
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert toggles == [True, False]
    assert enabled == {"contact_photos": True}
    assert json.loads(missing["value"]) == {"contact_photos": False}


@pytest.mark.private_dbus
def test_set_contact_photos_without_a_daemon_hook_is_not_ready() -> None:
    bus, name, service = _service()
    try:
        outcome = _call(name, MESSAGES_IFACE, "SetContactPhotos", True)
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert outcome["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"
