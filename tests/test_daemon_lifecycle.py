"""Daemon readiness and deferred hardware initialization."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.connectivity import Connectivity


@pytest.fixture(autouse=True)
def _ignore_the_hosts_installed_build_sha(monkeypatch):
    monkeypatch.setattr(daemon_mod, "installed_build_sha", lambda: None)


def _bare_daemon():
    instance = object.__new__(daemon_mod.Daemon)
    instance.sessions = object()
    instance.obex_worker = SimpleNamespace(submit=lambda *_args, **_kwargs: None)
    instance.contacts = SimpleNamespace(refresh=lambda: 0, count=lambda: 0)
    instance.connectivity = Connectivity()
    instance.notification_policy = SimpleNamespace(
        value="messages", contacts_only=False
    )
    instance.starred_threads = SimpleNamespace(
        keys=lambda: [],
        migrate=lambda: None,
        set_starred=lambda *_args, **_kwargs: False,
        discard=lambda *_args, **_kwargs: None,
        clear=lambda: None,
    )
    instance.confirmed_groups = SimpleNamespace(
        migrate=lambda: None,
        matches=lambda *_args, **_kwargs: False,
        matching_rosters=lambda _rosters: set(),
        remember=lambda *_args, **_kwargs: None,
        forget=lambda *_args, **_kwargs: None,
        clear=lambda: None,
    )
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
    instance._storage_retry_id = None
    instance._initializing = True
    instance._running_release = "0.6.0-6"
    instance._running_build_sha = None
    instance._running_build_id = "0.6.0-6"
    instance._release_missing_checks = 0
    instance._restart_after_upgrade = False
    instance.phone_audio = SimpleNamespace(reconcile=lambda **_kwargs: False)
    return instance


def test_start_publishes_dbus_before_scheduling_bluetooth(monkeypatch):
    instance = _bare_daemon()
    order = []
    scheduled = []
    periodic = []

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
        lambda delay, callback: periodic.append((delay, callback)) or len(periodic),
    )
    monkeypatch.setattr(daemon_mod.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        instance,
        "_initialize_bluetooth",
        lambda: order.append("bluetooth"),
    )
    monkeypatch.setattr(instance, "_initialize_storage", lambda: order.append("storage"))
    instance.phone_audio = SimpleNamespace(
        reconcile=lambda **_kwargs: order.append("audio")
    )

    instance.start()

    assert order == ["dirs", "claim", "service", "storage"]
    assert len(scheduled) == 1
    assert (daemon_mod.STORAGE_RETRY_SEC, instance._retry_storage) in periodic
    assert instance._initializing is True

    scheduled[0]()

    assert order == ["dirs", "claim", "service", "storage", "bluetooth"]
    assert instance._initializing is False


def test_stop_does_not_ask_obexd_to_remove_sessions(monkeypatch):
    instance = _bare_daemon()
    closed = []
    instance.adapter_class = SimpleNamespace(stop=lambda: None)
    instance.bearers = SimpleNamespace(stop=lambda: None)
    instance.profiles = SimpleNamespace(stop=lambda: None)
    instance.listener = None
    instance.ancs = None
    instance.solicitation = SimpleNamespace(stop=lambda: None)
    instance.events = SimpleNamespace(stop=lambda: None)
    instance._sleep_match = None
    instance.storage = SimpleNamespace(close=lambda: None)
    instance.sessions = SimpleNamespace(
        close_all=lambda **kwargs: closed.append(kwargs),
        stop_monitoring=lambda: None,
    )
    instance.obex_worker = SimpleNamespace(
        shutdown=lambda **kwargs: kwargs["cleanup"]()
    )
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)
    removed = []
    instance._storage_retry_id = 99
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)

    instance.stop()

    assert closed == [{"remove_remote": False}]
    assert removed == [99]
    assert instance._storage_retry_id is None


def test_storage_poll_survives_a_transient_scheduling_failure():
    instance = _bare_daemon()
    attempts = []

    def retry():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("wallet worker busy")

    instance._dbus_service = SimpleNamespace(retry_storage_unlock=retry)
    assert instance._retry_storage() is True
    assert instance._retry_storage() is True
    assert len(attempts) == 2


def test_startup_queues_storage_preparation_after_publishing_service():
    instance = _bare_daemon()
    calls = []
    instance._dbus_service = SimpleNamespace(
        retry_storage_unlock=lambda **kwargs: calls.append(kwargs),
    )
    instance._initialize_storage()
    assert calls == [{"initialize": True}]


def test_successful_empty_phonebook_verifies_contact_permission():
    instance = _bare_daemon()
    instance.contacts = SimpleNamespace(refresh=lambda: 0)
    verified = []
    instance._mark_setup_task = verified.append

    count = instance._contacts_pulled(0)

    assert count == 0
    assert verified == [daemon_mod.CONTACTS]


@pytest.mark.parametrize("ancs_connected", [False, True])
def test_status_exposes_split_ancs_and_last_le_error(monkeypatch, ancs_connected):
    instance = _bare_daemon()
    instance.contacts = SimpleNamespace(count=lambda: 0)
    instance.ancs = SimpleNamespace(
        connected=ancs_connected, subscribed=True, authorized=ancs_connected,
    )
    instance.bearers = SimpleNamespace(
        le_connected=False,
        snapshot=lambda: {
            "bredr": True,
            "le": False,
            "last_le_error": "org.bluez.Error.Failed",
            "last_le_error_message": "le-connection-abort-by-local",
        }
    )
    instance.setup_verification = SimpleNamespace(verified=())
    instance._mark_setup_task = lambda _task: False
    monkeypatch.setattr(daemon_mod, "history_count", lambda **_kwargs: 0)

    status = instance._status()

    assert status["ancs"] is ancs_connected
    assert status["ancs_subscribed"] is True
    assert status["ancs_authorized"] is ancs_connected
    assert status["contacts_only_notifications"] is False
    assert status["bredr"] is True
    assert status["le"] is ancs_connected
    assert status["last_le_error"] == "org.bluez.Error.Failed"
    assert status["last_le_error_message"] == "le-connection-abort-by-local"
    assert status["_build_id"] == "0.6.0-6"


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
    audio = []
    instance.phone_audio = SimpleNamespace(reconcile=lambda **kwargs: audio.append(kwargs))
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args: False)
    monkeypatch.setattr(
        daemon_mod.bluez_setup,
        "prepare",
        lambda: prepared.append(True),
    )

    with pytest.raises(daemon_mod.PairingRequiredError):
        instance._initialize_bluetooth()

    assert prepared == []
    assert audio == []


def test_bonded_start_reconciles_phone_audio_before_adapter_class(monkeypatch):
    instance = _bare_daemon()
    order = []
    monkeypatch.setattr(daemon_mod.config, "KEEP_PHONE_AUDIO_ON_PHONE", True)
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args: True)
    instance.phone_audio = SimpleNamespace(
        reconcile=lambda **kwargs: order.append(("audio", kwargs["enabled"]))
    )
    instance.adapter_class = SimpleNamespace(
        start=lambda: order.append("class") or (_ for _ in ()).throw(RuntimeError("stop"))
    )

    with pytest.raises(RuntimeError, match="stop"):
        instance._initialize_bluetooth()

    assert order == [("audio", True), "class"]


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


def test_changed_build_sha_restarts_the_packaged_daemon(monkeypatch):
    instance = _bare_daemon()
    instance._running_build_sha = "a" * 64
    instance._running_build_id = "0.6.0-6+sha." + "a" * 12
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(daemon_mod, "installed_build_sha", lambda: "b" * 64)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is False
    assert instance._restart_after_upgrade is True
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


def test_classic_reachable_accepts_an_open_obex_session() -> None:
    bearers = SimpleNamespace(bredr_connected=False)
    sessions = SimpleNamespace(map=object(), pbap=None)
    assert daemon_mod.classic_reachable(bearers, sessions) is True
    sessions = SimpleNamespace(map=None, pbap=None)
    assert daemon_mod.classic_reachable(bearers, sessions) is False
    bearers = SimpleNamespace(bredr_connected=True)
    assert daemon_mod.classic_reachable(bearers, sessions) is True
