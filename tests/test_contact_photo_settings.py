"""The saved contact-photo opt-in and switching it at runtime."""
from __future__ import annotations

import itertools
import json
import os

import dbus
import dbus.service
import pytest

from blueferry import config, contact_photo_settings
from blueferry.backend_operations import BackendDependencies
from blueferry.contact_photo_settings import ContactPhotoSettings, photos_enabled, set_runtime
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


def test_runtime_value_overrides_the_seed_until_reset(monkeypatch) -> None:
    monkeypatch.setattr(config, "CONTACT_PHOTOS", False)
    assert photos_enabled() is False
    set_runtime(True)
    assert photos_enabled() is True
    set_runtime(None)
    assert photos_enabled() is False


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
    assert contact_photo_settings.photos_enabled() is False
    assert daemon.photo_files is None and files == ["clear"]
    assert daemon._status()["contact_photos"] is False
    # Erasing runs on the OBEX worker, after any pull already queued there.
    [(job, done)] = jobs
    assert _photo_rows() == 1
    job()
    done(None)
    assert _photo_rows() == 0

    status = daemon._set_contact_photos(True)
    assert status["contact_photos"] is True
    assert contact_photo_settings.photos_enabled() is True
    assert daemon.photo_files is not None
    assert syncs == [True]
    assert len(statuses) >= 3


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
