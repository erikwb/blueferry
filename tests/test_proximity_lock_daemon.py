"""Daemon wiring of the proximity lock, against a real Daemon and fakes only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry import proximity_lock as pl
from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.errors import InvalidArgumentsError
from tests.test_proximity_lock import FakeClock, FakeLocker, FakeTimers


class Rig:
    """A real daemon whose bearer reads, timers, and lock call are fake."""

    def __init__(self, instance, *, enabled: bool = True, grace: int = 30) -> None:
        self.daemon = instance
        self.clock = FakeClock()
        self.timers = FakeTimers(self.clock)
        self.locker = FakeLocker()
        self.links: dict[str, bool | None] = {"bredr": None, "le": None}
        self.bearer_ticks: list = []
        instance.bearers = BearerSupervisor(
            "/org/bluez/hci0/dev_02_00_00_00_00_01",
            le_enabled=False,
            on_status=instance._bearer_status_changed,
            read_connected=self.links.get,
            connect=lambda *_args: None,
            schedule=lambda _delay, callback: self.bearer_ticks.append(callback) or 1,
            cancel=lambda _timer: None,
            clock=self.clock,
        )
        instance.proximity = pl.ProximityLock(
            enabled=enabled,
            grace_sec=grace,
            read_presence=instance._proximity_presence,
            locker=self.locker,
            on_status=instance._emit_status,
            schedule=self.timers.schedule,
            cancel=self.timers.cancel,
            clock=self.clock,
        )
        # Resume reconnects profiles; that path is not under test here.
        instance.profiles = SimpleNamespace(
            reconnect=lambda *_args, **_kwargs: None,
            stop=lambda: None,
        )
        instance.recovery = SimpleNamespace(
            active=False,
            invalidate=lambda **_kwargs: None,
            stop=lambda: None,
            forget_phone=lambda: None,
        )
        instance.bearers.start()

    def link(self, bredr: bool | None, le: bool | None = None) -> None:
        self.links["bredr"] = bredr
        self.links["le"] = le
        self.daemon.bearers.poke()


@pytest.fixture
def rig(make_daemon):
    return Rig(make_daemon())


def test_default_daemon_reports_disabled_and_never_locks(make_daemon) -> None:
    instance = make_daemon()
    assert instance.proximity.enabled is False
    snapshot = instance.proximity.snapshot()
    assert snapshot["proximity_lock"] == pl.STATE_DISABLED
    assert snapshot["proximity_lock_grace_sec"] == pl.DEFAULT_GRACE_SEC

    rig = Rig(instance, enabled=False)
    rig.link(True)
    rig.link(False)
    rig.timers.advance(3600)
    assert rig.locker.calls == 0


def test_bearer_transitions_drive_the_lock(rig) -> None:
    rig.link(True)
    assert rig.daemon.proximity.state == pl.STATE_ARMED
    rig.link(False, False)
    assert rig.daemon.proximity.state == pl.STATE_GRACE
    rig.timers.advance(29)
    assert rig.locker.calls == 0
    rig.timers.advance(1)
    assert rig.locker.calls == 1


def test_le_alone_keeps_the_phone_present(rig) -> None:
    rig.link(True, True)
    rig.link(False, True)
    rig.timers.advance(600)
    assert rig.locker.calls == 0
    assert rig.daemon.proximity.state == pl.STATE_ARMED


def test_suspend_and_resume_through_logind_signal(rig) -> None:
    rig.link(True)
    rig.daemon._on_prepare_for_sleep(True)
    rig.links["bredr"] = False  # link lost during suspend
    rig.timers.advance(3600)
    rig.daemon._on_prepare_for_sleep(False)
    rig.timers.advance(3600)
    assert rig.locker.calls == 0
    assert rig.daemon.proximity.state == pl.STATE_IDLE

    rig.link(True)
    assert rig.daemon.proximity.state == pl.STATE_ARMED


def test_desktop_bluetooth_off_and_discovery_suppress(rig) -> None:
    rig.link(True)
    rig.daemon._on_adapter_power_changed("org.bluez.Adapter1", {"Powered": False}, [])
    rig.link(False)
    rig.timers.advance(3600)
    assert rig.locker.calls == 0
    rig.daemon._on_adapter_power_changed("org.bluez.Adapter1", {"Powered": True}, [])
    rig.timers.advance(3600)
    assert rig.locker.calls == 0

    rig.link(True)
    rig.daemon._on_adapter_power_changed("org.bluez.Adapter1", {"Discovering": True}, [])
    rig.link(False)
    rig.timers.advance(3600)
    assert rig.locker.calls == 0
    assert "discovering" in rig.daemon.proximity.snapshot()["proximity_lock_inhibited"]


def test_forgetting_the_phone_cancels_a_pending_lock(rig, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon_mod.config, "current_target", lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)

    rig.link(True)
    rig.link(False)
    assert rig.daemon._check_target_config() is False
    rig.timers.advance(3600)
    assert rig.locker.calls == 0


def test_clearing_the_target_cancels_a_pending_lock(rig, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.config, "current_target", lambda: ("", "hci0"))
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)
    rig.link(True)
    rig.link(False)
    rig.daemon._check_target_config()
    rig.timers.advance(3600)
    assert rig.locker.calls == 0


def test_own_adapter_recovery_does_not_lock(rig) -> None:
    rig.link(True)
    rig.daemon._bluetooth_initialized = False  # skip real supervisor restarts
    rig.daemon._pause_for_recovery()
    rig.link(False)
    rig.timers.advance(3600)
    assert rig.locker.calls == 0
    rig.daemon._initialization_retry_id = 1  # a restart is already queued
    rig.daemon._resume_after_recovery()
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == ""
    rig.timers.advance(3600)
    assert rig.locker.calls == 0


def test_bluez_restart_forgets_presence(rig, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.bluez_setup, "forget_advert_registration", lambda: None)
    rig.daemon.solicitation = SimpleNamespace(reset_after_bluez_restart=lambda: None)
    rig.daemon._bluez_owner_match = object()  # watching, without a real bus
    rig.link(True)
    rig.link(False)
    rig.daemon._on_bluez_owner_changed("org.bluez", ":1.1", "")
    rig.timers.advance(3600)
    assert rig.locker.calls == 0
    assert rig.daemon.proximity.state == pl.STATE_IDLE


def test_status_and_setter_round_trip(rig, isolated_state) -> None:
    rig.daemon.proximity.configure(False, 60)
    rig.daemon.proximity_settings = pl.ProximityLockSettings(default_enabled=False)
    status = rig.daemon._status()
    assert status["proximity_lock"] == pl.STATE_DISABLED
    json.dumps(status)

    snapshot = rig.daemon._set_proximity_lock(True, 45)
    assert snapshot["proximity_lock_enabled"] is True
    assert snapshot["proximity_lock_grace_sec"] == 45
    assert pl.ProximityLockSettings(default_enabled=False).enabled is True

    with pytest.raises(ValueError):
        rig.daemon._set_proximity_lock(True, 5)


def test_backend_operation_maps_validation_errors() -> None:
    from blueferry.backend_operations import BackendDependencies, BackendOperations

    def configure(enabled, grace):
        if grace < pl.MIN_GRACE_SEC:
            raise ValueError("too short")
        return {"proximity_lock_enabled": enabled, "proximity_lock_grace_sec": grace}

    sessions = SimpleNamespace(map=None, pbap=None, map_path="")
    operations = BackendOperations(
        sessions, BackendDependencies(set_proximity_lock=configure)
    )
    assert operations.set_proximity_lock(True, 30)["proximity_lock_grace_sec"] == 30
    with pytest.raises(InvalidArgumentsError):
        operations.set_proximity_lock(True, 1)


def test_stop_disarms(rig) -> None:
    rig.link(True)
    rig.link(False)
    rig.daemon.proximity.stop()
    rig.timers.advance(3600)
    assert rig.locker.calls == 0


class _AdapterBus:
    """Records asynchronous Adapter1 GetAll calls; the test answers them."""

    def __init__(self) -> None:
        self.pending: list[tuple] = []

    def call_async(self, service, path, interface, method, signature, args,
                   reply, error, timeout=None):
        assert (service, interface, method, args) == (
            "org.bluez", "org.freedesktop.DBus.Properties", "GetAll",
            ("org.bluez.Adapter1",),
        )
        self.pending.append((reply, error))


def _restartable(rig, monkeypatch) -> _AdapterBus:
    bus = _AdapterBus()
    monkeypatch.setattr(daemon_mod, "get_system_bus", lambda: bus)
    monkeypatch.setattr(daemon_mod.bluez_setup, "forget_advert_registration", lambda: None)
    rig.daemon.solicitation = SimpleNamespace(reset_after_bluez_restart=lambda: None)
    rig.daemon.adapter_class = SimpleNamespace(poke=lambda: None, stop=lambda: None)
    rig.daemon._bluez_owner_match = object()
    return bus


def test_bluetoothd_loss_clears_adapter_inhibitors(rig, monkeypatch) -> None:
    _restartable(rig, monkeypatch)
    changed = rig.daemon._on_adapter_power_changed
    changed("org.bluez.Adapter1", {"Powered": False, "Discovering": True}, [])
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == (
        "adapter-off,discovering"
    )
    # bluetoothd exits: its PropertiesChanged "off/discovering" can never be
    # withdrawn by the process that set it.
    rig.daemon._on_bluez_owner_changed("org.bluez", ":1.1", "")
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == ""


def test_bluetoothd_restart_reseeds_adapter_inhibitors(rig, monkeypatch) -> None:
    bus = _restartable(rig, monkeypatch)
    rig.daemon._on_adapter_power_changed("org.bluez.Adapter1", {"Discovering": True}, [])
    rig.daemon._on_bluez_owner_changed("org.bluez", ":1.1", ":1.2")
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == ""
    assert len(bus.pending) == 1

    reply, _error = bus.pending.pop()
    reply({"Powered": True, "Discovering": True, "Address": "ignored"})
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == "discovering"

    rig.daemon._on_bluez_owner_changed("org.bluez", ":1.2", ":1.3")
    stale, _error = bus.pending.pop(0)
    rig.daemon._on_bluez_owner_changed("org.bluez", ":1.3", ":1.4")
    stale({"Powered": False})  # answered by a superseded bluetoothd
    assert "adapter-off" not in rig.daemon.proximity.snapshot()["proximity_lock_inhibited"]
    _current, failed = bus.pending.pop()
    failed(RuntimeError("no adapter"))  # tolerated
    assert rig.daemon.proximity.snapshot()["proximity_lock_inhibited"] == ""
