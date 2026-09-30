"""Daemon readiness and deferred hardware initialization."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod


def _idle_recovery():
    """Recovery supervision watches the system bus; these tests run without it."""
    return SimpleNamespace(
        active=False, start=lambda: None,
        adapter=SimpleNamespace(restore_pending=False, cleanup_pending=False),
    )


def test_start_publishes_dbus_before_scheduling_bluetooth(make_daemon, monkeypatch):
    instance = make_daemon()
    order = []
    scheduled = []
    periodic = []
    instance.recovery.adapter.reload_journal = lambda: order.append("recovery")

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
    monkeypatch.setattr(instance, "_retry_storage", lambda: order.append("retry") or True)
    instance.phone_audio = SimpleNamespace(
        reconcile=lambda **_kwargs: order.append("audio")
    )

    instance.start()

    assert order == ["dirs", "claim", "recovery", "service", "storage"]
    assert len(scheduled) == 1
    storage_ticks = [tick for delay, tick in periodic if delay == daemon_mod.STORAGE_RETRY_SEC]
    assert len(storage_ticks) == 1
    assert storage_ticks[0]() is True
    assert order[-1] == "retry"
    del order[-1]
    assert instance._initializing is True

    scheduled[0]()

    assert order == ["dirs", "claim", "recovery", "service", "storage", "bluetooth"]
    assert instance._initializing is False


def test_stop_does_not_ask_obexd_to_remove_sessions(make_daemon, monkeypatch):
    instance = make_daemon()
    closed = []
    instance.adapter_class = SimpleNamespace(stop=lambda: None)
    instance.bearers = SimpleNamespace(stop=lambda: None)
    instance.profiles = SimpleNamespace(stop=lambda: None)
    instance.listener = None
    instance.mns_watch = None
    instance.ancs = None
    instance.solicitation = SimpleNamespace(stop=lambda: None)
    instance.events = SimpleNamespace(stop=lambda: None)
    instance._sleep_match = None
    owner_watches_removed = []
    instance._bluez_owner_match = SimpleNamespace(remove=lambda: owner_watches_removed.append(True))
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
    contact_timers_stopped = []
    instance._storage_retry_id = 99
    monkeypatch.setattr(
        instance.contact_sync, "stop", lambda: contact_timers_stopped.append(True),
    )
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)

    instance.stop()

    assert closed == [{"remove_remote": False}]
    assert removed == [99]
    assert contact_timers_stopped == [True]
    assert instance._storage_retry_id is None
    assert owner_watches_removed == [True]
    assert instance._bluez_owner_match is None
    instance._on_bluez_owner_changed('org.bluez', ':1.1', ':1.2')
    assert instance._bluez_owner_generation == 0


@pytest.mark.parametrize("owns_name", [False, True])
def test_failed_startup_only_restores_bluetooth_for_bus_owner(make_daemon, monkeypatch, owns_name):
    instance = make_daemon()
    calls = []
    instance.recovery.adapter.reload_journal = lambda: calls.append("reload")
    instance.recovery.adapter.finish_shutdown = lambda: calls.append("restore")
    for name in ("adapter_class", "bearers", "profiles", "solicitation"):
        setattr(instance, name, SimpleNamespace(stop=lambda: None))
    instance.events.stop = lambda: None
    instance.listener = None
    instance.ancs = None
    instance._sleep_match = None
    instance.storage.close = lambda: None
    instance.sessions = SimpleNamespace(
        close_all=lambda **_kwargs: calls.append("close-local-sessions"),
        stop_monitoring=lambda: None,
    )
    instance.obex_worker = SimpleNamespace(
        submit=instance.obex_worker.submit,
        shutdown=lambda **kwargs: kwargs["cleanup"](),
    )
    monkeypatch.setattr(daemon_mod.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)

    def fail(*_args, **_kwargs):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(daemon_mod, "claim_bus_name", (lambda: object()) if owns_name else fail)
    monkeypatch.setattr(daemon_mod, "MessagesService", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        instance.run()
    assert calls == (["reload", "restore", "close-local-sessions"] if owns_name
                     else ["close-local-sessions"])


def test_storage_poll_survives_a_transient_scheduling_failure(make_daemon):
    instance = make_daemon()
    attempts = []

    def retry():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("wallet worker busy")

    instance._dbus_service = SimpleNamespace(retry_storage_unlock=retry)
    assert instance._retry_storage() is True
    assert instance._retry_storage() is True
    assert len(attempts) == 2


def test_startup_queues_storage_preparation_after_publishing_service(make_daemon):
    instance = make_daemon()
    calls = []
    instance._dbus_service = SimpleNamespace(
        retry_storage_unlock=lambda **kwargs: calls.append(kwargs),
    )
    instance._initialize_storage()
    assert calls == [{"initialize": True}]


def test_completed_phonebook_pull_verifies_contact_permission(make_daemon):
    instance = make_daemon()

    instance._contacts_refreshed()

    assert daemon_mod.CONTACTS in instance.setup_verification.verified


def test_status_exposes_split_ancs_and_last_le_error(make_daemon, monkeypatch):
    instance = make_daemon()
    instance.contacts = SimpleNamespace(count=lambda: 0)
    instance.ancs = SimpleNamespace(
        connected=False, subscribed=True, authorized=False,
    )
    instance.bearers = SimpleNamespace(
        snapshot=lambda: {
            "bredr": True,
            "le": False,
            "last_le_error": "org.bluez.Error.Failed",
            "last_le_error_message": "le-connection-abort-by-local",
        }
    )
    instance.setup_verification = SimpleNamespace(verified=())
    monkeypatch.setattr(daemon_mod, "history_count", lambda **_kwargs: 0)

    status = instance._status()

    assert status["ancs"] is False
    assert status["ancs_subscribed"] is True
    assert status["ancs_authorized"] is False
    assert status["contacts_only_notifications"] is False
    assert status["otp_autocopy"] is daemon_mod.config.OTP_AUTOCOPY
    assert isinstance(status["otp_autocopy"], bool)
    assert status["bredr"] is True
    assert status["le"] is False
    assert status["last_le_error"] == "org.bluez.Error.Failed"
    assert status["last_le_error_message"] == "le-connection-abort-by-local"
    assert status["_build_id"] == "0.6.0-6"


def test_failed_hardware_initialization_leaves_control_service_alive(make_daemon, monkeypatch):
    instance = make_daemon()
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


def test_missing_bond_never_prepares_or_connects_bluetooth(make_daemon, monkeypatch):
    instance = make_daemon()
    instance.recovery = _idle_recovery()
    prepared = []
    audio = []
    instance.phone_audio = SimpleNamespace(reconcile=lambda **kwargs: audio.append(kwargs))
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        daemon_mod.bluez_setup,
        "prepare_classic",
        lambda: prepared.append(True),
    )

    with pytest.raises(daemon_mod.PairingRequiredError):
        instance._initialize_bluetooth()

    assert prepared == []
    assert audio == []


def test_bonded_start_reconciles_phone_audio_before_adapter_class(make_daemon, monkeypatch):
    instance = make_daemon()
    instance.recovery = _idle_recovery()
    order = []
    monkeypatch.setattr(daemon_mod.config, "KEEP_PHONE_AUDIO_ON_PHONE", True)
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: True)
    instance.phone_audio = SimpleNamespace(
        reconcile=lambda **kwargs: order.append(("audio", kwargs["enabled"]))
    )
    instance.adapter_class = SimpleNamespace(
        start=lambda: order.append("class") or (_ for _ in ()).throw(RuntimeError("stop"))
    )

    with pytest.raises(RuntimeError, match="stop"):
        instance._initialize_bluetooth()

    assert order == [("audio", True), "class"]


def test_transient_missing_release_marker_does_not_stop_daemon(make_daemon, monkeypatch):
    instance = make_daemon()
    releases = iter([None, "0.6.0-6"])
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: next(releases))
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is True
    assert instance._check_package_release() is True
    assert instance._release_missing_checks == 0
    assert stopped == []


def test_persistent_missing_release_marker_stops_cleanly(make_daemon, monkeypatch):
    instance = make_daemon()
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: None)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is True
    assert instance._check_package_release() is True
    assert instance._check_package_release() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_changed_build_sha_restarts_the_packaged_daemon(make_daemon, monkeypatch):
    instance = make_daemon()
    instance._running_build_sha = "a" * 64
    instance._running_build_id = "0.6.0-6+sha." + "a" * 12
    stopped = []
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(daemon_mod, "installed_build_sha", lambda: "b" * 64)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_package_release() is False
    assert instance._restart_after_upgrade is True
    assert stopped == [True]


def test_clearing_saved_target_stops_daemon_without_restart(make_daemon, monkeypatch):
    instance = make_daemon()
    stopped = []
    monkeypatch.setattr(daemon_mod.config, "current_target", lambda: ("", "hci0"))
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_removing_bond_stops_active_daemon_without_restart(make_daemon, monkeypatch):
    instance = make_daemon()
    stopped = []
    monkeypatch.setattr(
        daemon_mod.config,
        "current_target",
        lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is False
    assert instance._restart_after_upgrade is False
    assert stopped == [True]


def test_transient_bond_inspection_failure_keeps_daemon_running(make_daemon, monkeypatch):
    instance = make_daemon()
    stopped = []
    monkeypatch.setattr(
        daemon_mod.config,
        "current_target",
        lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: stopped.append(True))

    assert instance._check_target_config() is True
    assert stopped == []


def test_changing_saved_target_requests_restart(make_daemon, monkeypatch):
    instance = make_daemon()
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


def _schedule_with_id_42(instance, monkeypatch, timer_attr, callback):
    """Register ``callback`` like start() does and return GLib's tick."""
    scheduled = []

    def timeout_add_seconds(_seconds, tick):
        scheduled.append(tick)
        return 42

    monkeypatch.setattr(daemon_mod.GLib, "timeout_add_seconds", timeout_add_seconds)
    instance._schedule_periodic(timer_attr, 1, getattr(instance, callback))
    assert getattr(instance, timer_attr) == 42
    (tick,) = scheduled
    return tick


def _stop_saved_target_by_clearing(_instance, monkeypatch):
    monkeypatch.setattr(daemon_mod.config, "current_target", lambda: ("", "hci0"))


def _stop_saved_target_by_changing(_instance, monkeypatch):
    monkeypatch.setattr(
        daemon_mod.config, "current_target", lambda: ("02:00:00:00:00:02", "hci1"),
    )


def _stop_saved_target_by_removing_bond(instance, monkeypatch):
    # Keep the ANCS recovery budget out of the settings file.
    monkeypatch.setattr(instance.recovery, "forget_phone", lambda: None)
    monkeypatch.setattr(
        daemon_mod.config, "current_target", lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: False)


def _stop_package_by_removing_marker(_instance, monkeypatch):
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: None)


def _stop_package_by_upgrading(_instance, monkeypatch):
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: "0.6.0-7")


@pytest.mark.parametrize(
    ("timer_attr", "callback", "arrange"),
    [
        ("_target_config_check_id", "_check_target_config", _stop_saved_target_by_clearing),
        ("_target_config_check_id", "_check_target_config", _stop_saved_target_by_changing),
        ("_target_config_check_id", "_check_target_config",
         _stop_saved_target_by_removing_bond),
        ("_release_check_id", "_check_package_release", _stop_package_by_removing_marker),
        ("_release_check_id", "_check_package_release", _stop_package_by_upgrading),
    ],
)
def test_stop_does_not_remove_a_timer_that_stopped_itself(
    make_daemon, monkeypatch, timer_attr, callback, arrange,
):
    # Returning False makes GLib destroy the source. Removing its ID again
    # during shutdown makes GLib warn "Source ID ... was not found".
    instance = make_daemon()
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)
    arrange(instance, monkeypatch)
    removed = []
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)
    tick = _schedule_with_id_42(instance, monkeypatch, timer_attr, callback)

    # A missing release marker needs consecutive misses before it stops.
    for _ in range(3):
        if not tick():
            break
    else:
        pytest.fail(f"{callback} never stopped itself")
    instance.stop()

    # Other components may own timers of their own; only ours matters here.
    assert 42 not in removed
    assert getattr(instance, timer_attr) is None


@pytest.mark.parametrize(
    ("timer_attr", "callback", "target"),
    [
        ("_release_check_id", "_check_package_release", "installed_release"),
        ("_target_config_check_id", "_check_target_config", "bond_status"),
    ],
)
def test_stop_does_not_remove_a_timer_whose_callback_raised(
    make_daemon, monkeypatch, timer_attr, callback, target,
):
    # PyGObject logs the exception and destroys the source, as for False.
    instance = make_daemon()
    monkeypatch.setattr(
        daemon_mod.config, "current_target", lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")

    def fail(*_args, **_kwargs):
        raise ValueError("half-written marker")

    monkeypatch.setattr(daemon_mod, target, fail)
    removed = []
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)
    tick = _schedule_with_id_42(instance, monkeypatch, timer_attr, callback)

    with pytest.raises(ValueError, match="half-written marker"):
        tick()
    assert getattr(instance, timer_attr) is None
    instance.stop()

    assert 42 not in removed


def test_stop_removes_a_target_check_that_keeps_running(make_daemon, monkeypatch):
    instance = make_daemon()
    monkeypatch.setattr(
        daemon_mod.config, "current_target", lambda: ("02:00:00:00:00:01", "hci0"),
    )
    monkeypatch.setattr(daemon_mod.config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(daemon_mod.config, "ADAPTER", "hci0")
    monkeypatch.setattr(daemon_mod, "bond_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daemon_mod.main_loop, "quit", lambda: None)
    removed = []
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)
    tick = _schedule_with_id_42(
        instance, monkeypatch, "_target_config_check_id", "_check_target_config",
    )

    assert tick() is True
    instance.stop()

    assert removed.count(42) == 1
    assert instance._target_config_check_id is None


def test_stop_removes_a_release_check_that_keeps_running(make_daemon, monkeypatch):
    instance = make_daemon()
    removed = []
    monkeypatch.setattr(daemon_mod.GLib, "source_remove", removed.append)
    tick = _schedule_with_id_42(
        instance, monkeypatch, "_release_check_id", "_check_package_release",
    )

    # make_daemon reports the release the daemon is running.
    assert tick() is True
    assert instance._release_check_id == 42
    instance.stop()

    assert removed.count(42) == 1
    assert instance._release_check_id is None


def test_classic_reachable_accepts_an_open_obex_session() -> None:
    bearers = SimpleNamespace(bredr_connected=False)
    sessions = SimpleNamespace(map=object(), pbap=None)
    assert daemon_mod.classic_reachable(bearers, sessions) is True
    sessions = SimpleNamespace(map=None, pbap=None)
    assert daemon_mod.classic_reachable(bearers, sessions) is False
    bearers = SimpleNamespace(bredr_connected=True)
    assert daemon_mod.classic_reachable(bearers, sessions) is True


def test_mns_loss_reconnects_map_once_per_outage():
    from types import SimpleNamespace

    from blueferry import daemon

    reconnects = []
    instance = daemon.Daemon.__new__(daemon.Daemon)
    instance.profiles = SimpleNamespace(reconnect=reconnects.append)
    instance._mns_reconnect_spent = False

    instance._mns_missing("the iPhone closed MAP notifications")
    # The fresh MAP session did not bring MNS back either.
    instance._mns_missing("the iPhone did not open MAP notifications")
    assert reconnects == ["the iPhone closed MAP notifications"]

    instance._mns_present()
    instance._mns_missing("the iPhone closed MAP notifications")
    assert reconnects == ["the iPhone closed MAP notifications"] * 2
