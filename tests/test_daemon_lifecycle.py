"""Daemon readiness and deferred hardware initialization."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.connectivity import Connectivity


def _bare_daemon():
    instance = object.__new__(daemon_mod.Daemon)
    instance.sessions = object()
    instance.obex_worker = SimpleNamespace(submit=lambda *_args, **_kwargs: None)
    instance.contacts = object()
    instance.connectivity = Connectivity()
    instance.notification_policy = SimpleNamespace(value="messages")
    storage_status = SimpleNamespace(
        policy="encrypted", state="ready", detail="", can_read=True
    )
    instance.storage = SimpleNamespace(
        status=storage_status,
        refresh=lambda **_kwargs: storage_status,
    )
    instance.events = SimpleNamespace(
        sent=lambda *_args: None,
        group_sent=lambda *_args: None,
        set_dbus_service=lambda *_args: None,
    )
    instance._bus_name = None
    instance._dbus_service = None
    instance._packaged = False
    instance._startup_id = None
    instance._initialization_retry_id = None
    instance._target_config_check_id = None
    instance._initializing = True
    instance._running_release = "0.6.0-6"
    instance._release_missing_checks = 0
    instance._restart_after_upgrade = False
    return instance


def test_start_publishes_dbus_before_scheduling_bluetooth(monkeypatch):
    instance = _bare_daemon()
    order = []
    scheduled = []

    monkeypatch.setattr(daemon_mod.config, "ensure_dirs", lambda: order.append("dirs"))
    monkeypatch.setattr(
        daemon_mod,
        "claim_bus_name",
        lambda: order.append("claim") or object(),
    )
    monkeypatch.setattr(
        daemon_mod,
        "MessagesService",
        lambda *_args, **_kwargs: order.append("service") or object(),
    )
    monkeypatch.setattr(
        daemon_mod.GLib,
        "timeout_add",
        lambda _delay, callback: scheduled.append(callback) or 1,
    )
    monkeypatch.setattr(
        daemon_mod.GLib,
        "timeout_add_seconds",
        lambda _delay, _callback: 2,
    )
    monkeypatch.setattr(daemon_mod.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        instance,
        "_initialize_bluetooth",
        lambda: order.append("bluetooth"),
    )
    monkeypatch.setattr(instance, "_initialize_storage", lambda: order.append("storage"))

    instance.start()

    assert order == ["dirs", "claim", "storage", "service"]
    assert len(scheduled) == 1
    assert instance._initializing is True

    scheduled[0]()

    assert order[-1] == "bluetooth"
    assert instance._initializing is False


def test_no_storage_policy_reasserts_empty_local_data(monkeypatch):
    instance = _bare_daemon()
    status = SimpleNamespace(
        policy="none", state="disabled", detail="", can_read=False
    )
    instance.storage = SimpleNamespace(
        status=status,
        refresh=lambda **_kwargs: status,
    )
    cleared = []
    monkeypatch.setattr(
        daemon_mod, "clear_events", lambda: cleared.append("events")
    )
    monkeypatch.setattr(
        daemon_mod, "clear_contact_cache", lambda: cleared.append("contacts")
    )

    instance._initialize_storage()

    assert cleared == ["events", "contacts"]


def test_failed_hardware_initialization_leaves_control_service_alive(monkeypatch):
    instance = _bare_daemon()
    scheduled = []

    def fail():
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(instance, "_initialize_bluetooth", fail)
    monkeypatch.setattr(
        daemon_mod.GLib,
        "timeout_add_seconds",
        lambda delay, callback: scheduled.append((delay, callback)) or 1,
    )

    assert instance._initialize() is False
    assert instance._initializing is False
    assert instance.connectivity.snapshot()["connectivity_state"] == "degraded"
    assert scheduled[0][0] == 5


def test_missing_bond_never_prepares_or_connects_bluetooth(monkeypatch):
    instance = _bare_daemon()
    prepared = []
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args: False)
    monkeypatch.setattr(
        daemon_mod.bluez_setup,
        "prepare",
        lambda: prepared.append(True),
    )

    with pytest.raises(daemon_mod.PairingRequiredError):
        instance._initialize_bluetooth()

    assert prepared == []


def test_transient_missing_release_marker_does_not_stop_daemon(monkeypatch):
    instance = _bare_daemon()
    releases = iter([None, "0.6.0-6"])
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: next(releases))
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is True
    assert instance._check_package_release() is True
    assert instance._release_missing_checks == 0
    assert stopped == []


def test_persistent_missing_release_marker_stops_cleanly(monkeypatch):
    instance = _bare_daemon()
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: None)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is True
    assert instance._check_package_release() is True
    assert instance._check_package_release() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_clearing_saved_target_stops_daemon_without_restart(monkeypatch):
    instance = _bare_daemon()
    stopped = []
    monkeypatch.setattr(daemon_mod.config, "current_target", lambda: ("", "hci0"))
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_removing_bond_stops_active_daemon_without_restart(monkeypatch):
    instance = _bare_daemon()
    stopped = []
    monkeypatch.setattr(
        daemon_mod.config,
        "current_target",
        lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args: False)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_transient_bond_inspection_failure_keeps_daemon_running(monkeypatch):
    instance = _bare_daemon()
    stopped = []
    monkeypatch.setattr(
        daemon_mod.config,
        "current_target",
        lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args: None)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is True
    assert stopped == []


def test_changing_saved_target_requests_restart(monkeypatch):
    instance = _bare_daemon()
    stopped = []
    monkeypatch.setattr(
        daemon_mod.config,
        "current_target",
        lambda: ("02:00:00:00:00:02", "hci1"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is False
    assert instance._restart_after_upgrade is True
    assert stopped == [True]
