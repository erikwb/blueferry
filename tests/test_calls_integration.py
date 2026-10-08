"""Opt-in configuration and daemon wiring of the optional calls feature."""
from __future__ import annotations

import pytest

from blueferry import config
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.errors import CallsDisabledError
from blueferry.models import BackendStatus, CallsSnapshot


class _Sessions:
    map = None
    pbap = None
    map_path = ""

    @staticmethod
    def report_error(_error) -> None:
        pass


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("maybe", False), ("false", False), ("0", False),
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), (" on ", True),
])
def test_calls_are_strictly_opt_in(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("BLUEFERRY_CALLS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BLUEFERRY_CALLS_ENABLED", value)

    assert config._env_opt_in("BLUEFERRY_CALLS_ENABLED") is expected


def test_calls_flag_is_read_from_local_env(tmp_path) -> None:
    path = tmp_path / "local.env"
    path.write_text("BLUEFERRY_CALLS_ENABLED=true\nBLUEFERRY_UNKNOWN=1\n")
    path.chmod(0o600)

    assert config.read_local_env(path) == {"BLUEFERRY_CALLS_ENABLED": "true"}


def test_default_daemon_keeps_calls_disabled_and_inert(make_daemon) -> None:
    instance = make_daemon()

    assert instance.calls.enabled is False
    instance.calls.start()
    assert instance.calls.state == "disabled"
    status = BackendOperations(
        _Sessions(), BackendDependencies(calls=instance.calls),
    ).status()
    # Off: only the opt-in itself is visible, no call state.
    assert status["calls_enabled"] is False
    assert "calls_state" not in status and "calls_available" not in status
    with pytest.raises(CallsDisabledError):
        BackendOperations(_Sessions(), BackendDependencies(calls=instance.calls)).list_calls()


def test_enabled_daemon_constructs_calls_without_bus_io(make_daemon, monkeypatch) -> None:
    from blueferry import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.config, "CALLS_ENABLED", True)
    instance = make_daemon()

    # The harness makes any system-bus access fatal; construction must not
    # have created the oFono transport yet.
    assert instance.calls.enabled is True
    assert instance.calls._transport is None
    assert instance.calls.snapshot()["calls_state"] == "unavailable"


def test_bearer_status_pokes_calls_and_publishes_status(make_daemon) -> None:
    instance = make_daemon()
    seen = []
    instance.calls.poke = lambda: seen.append("poke")
    instance.proximity.bearer_changed = lambda: seen.append("proximity")
    instance._emit_status = lambda: seen.append("status")

    instance._bearer_status_changed()

    assert seen == ["proximity", "poke", "status"]


def test_daemon_stop_stops_calls(make_daemon, monkeypatch) -> None:
    instance = make_daemon()
    stopped = []
    instance.calls.stop = lambda: stopped.append(True)
    monkeypatch.setattr("blueferry.daemon.main_loop.quit", lambda: None)

    instance.stop()

    assert stopped == [True]


def test_status_model_decodes_call_fields() -> None:
    status = BackendStatus.from_dict({
        "calls_enabled": True, "calls_state": "ready", "calls_available": True,
    })

    assert (status.calls_enabled, status.calls_state, status.calls_available) == (
        True, "ready", True,
    )
    assert status.to_dict()["calls_state"] == "ready"
    assert BackendStatus.from_dict({}).calls_state == "disabled"


def test_status_model_keeps_an_unreported_calls_setting_absent() -> None:
    # A daemon that predates the setting: clients must not offer the opt-in.
    older = BackendStatus.from_dict({"daemon": True})

    assert older.calls_enabled is None
    assert "calls_enabled" not in older.to_dict()
    assert "calls_enabled" not in BackendStatus().to_dict()
    for reported in (False, True):
        status = BackendStatus.from_dict({"daemon": True, "calls_enabled": reported})
        assert status.to_dict()["calls_enabled"] is reported


def test_calls_snapshot_ignores_malformed_entries() -> None:
    snapshot = CallsSnapshot.from_dict({
        "state": "ready",
        "calls": [{"call_id": "voicecall01", "state": "incoming", "number": "123"}, 5, {}],
    })

    assert snapshot.available
    assert [call.call_id for call in snapshot.calls] == ["voicecall01"]
    assert snapshot.calls[0].ringing and snapshot.calls[0].display_peer == "123"


def test_controller_changes_reach_the_bus_signals(make_daemon) -> None:
    from types import SimpleNamespace

    instance = make_daemon()
    emitted = []
    instance._dbus_service = SimpleNamespace(
        emit_calls_changed=lambda: emitted.append("calls"),
        emit_status=lambda: emitted.append("status"),
    )

    # StatusChanged from the calls controller is deferred to the main loop.
    instance._idle_add = lambda callback, **_options: callback()
    # The controller holds the daemon's callbacks from construction.
    instance.calls._on_calls_changed()
    instance.calls._on_state_changed()

    assert emitted == ["calls", "status"]


def test_bluetooth_recovery_is_held_back_during_a_call(make_daemon) -> None:
    instance = make_daemon()

    assert instance.bearers.busy is False
    assert instance._recovery_observation().busy is False
    instance.calls._calls["voicecall01"] = object()
    assert instance.calls.in_call is True
    assert instance._recovery_observation().busy is True
    instance.calls._calls.clear()
    assert instance._recovery_observation().busy is False


def test_daemon_follows_the_saved_opt_in_over_local_env(make_daemon, monkeypatch) -> None:
    from blueferry import daemon as daemon_mod
    from blueferry.calls.settings import CallsSettings

    monkeypatch.setattr(daemon_mod.config, "CALLS_ENABLED", False)
    CallsSettings().set(True)
    instance = make_daemon()

    assert instance.calls.enabled is True
    assert instance.phone_audio.allow_calls is True


def test_switching_calls_at_runtime_saves_applies_and_rewrites_roles(
    make_daemon, monkeypatch,
) -> None:
    from blueferry import daemon as daemon_mod
    from blueferry.calls.settings import CallsSettings

    monkeypatch.setattr(daemon_mod.config, "KEEP_PHONE_AUDIO_ON_PHONE", True)
    instance = make_daemon()
    started, applied, statuses = [], [], []
    instance.calls.start = lambda: started.append(True)
    instance._emit_status = lambda: statuses.append(True)
    instance._idle_add = lambda callback, **_options: callback()

    class FakePolicy:
        def __init__(self, *, allow_calls):
            self.allow_calls = allow_calls

        def reconcile(self, *, enabled):
            applied.append((self.allow_calls, enabled))
            return True

    background = []

    def inline(target, name):
        background.append(name)
        target()

    monkeypatch.setattr(daemon_mod, "WirePlumberPhoneAudioPolicy", FakePolicy)
    monkeypatch.setattr(daemon_mod, "_in_background", inline)

    status = instance._set_calls_enabled(True)

    assert status["calls_enabled"] is True
    assert started == [True] and statuses
    assert CallsSettings().enabled is True
    assert applied == [(True, True)]
    assert background == ["blueferry-phone-audio"]

    instance._set_calls_enabled(False)
    assert instance.calls.enabled is False
    assert CallsSettings().enabled is False
    assert applied[-1] == (False, True)


def test_quick_toggles_leave_the_roles_matching_the_saved_setting(
    make_daemon, monkeypatch,
) -> None:
    from blueferry import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.config, "KEEP_PHONE_AUDIO_ON_PHONE", True)
    instance = make_daemon()
    instance.calls.start = lambda: None
    # Status emission is coalesced through an idle callback.
    instance._idle_add = lambda callback, **_options: callback()
    instance._emit_status = lambda: None
    applied, queued = [], []

    class FakePolicy:
        def __init__(self, *, allow_calls):
            self.allow_calls = allow_calls

        def reconcile(self, *, enabled):
            applied.append(self.allow_calls)
            return True

    monkeypatch.setattr(daemon_mod, "WirePlumberPhoneAudioPolicy", FakePolicy)
    monkeypatch.setattr(daemon_mod, "_in_background", lambda target, _name: queued.append(target))

    instance._set_calls_enabled(True)
    instance._set_calls_enabled(False)
    assert len(queued) == 2 and applied == []

    # The thread started for "on" gets the lock last.
    queued[1]()
    queued[0]()

    assert applied[-1] is False
    assert instance.phone_audio.allow_calls is False


def test_switching_calls_leaves_wireplumber_alone_without_the_audio_policy(
    make_daemon, monkeypatch,
) -> None:
    from blueferry import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.config, "KEEP_PHONE_AUDIO_ON_PHONE", False)
    instance = make_daemon()
    instance.calls.start = lambda: None
    instance._idle_add = lambda callback, **_options: callback()
    instance._emit_status = lambda: None
    def forbidden(_target, _name):
        raise AssertionError("no reconcile expected")

    monkeypatch.setattr(daemon_mod, "_in_background", forbidden)

    instance._set_calls_enabled(True)


def _conflict_daemon(make_daemon, monkeypatch, *, argv, version):
    from blueferry import daemon as daemon_mod

    background, commands = [], []

    def stack(*, run_command, experimental):
        commands.append(experimental)
        return {"bluez_version": version} if version else {}

    monkeypatch.setattr(daemon_mod, "bluetoothd_argv", lambda: argv)
    monkeypatch.setattr(daemon_mod, "bluez_stack", stack)
    monkeypatch.setattr(daemon_mod, "_in_background", lambda target, _name: background.append(target))
    return make_daemon(), background, commands


def test_hfp_conflict_needs_the_plugin_options_and_a_bluez_that_has_it(
    make_daemon, monkeypatch,
) -> None:
    instance, background, commands = _conflict_daemon(
        make_daemon, monkeypatch, argv=["bluetoothd", "-E"], version="5.87",
    )

    # The version lookup never runs on the main loop: unknown is "no" first.
    assert instance._bluez_hfp_conflict() is False
    assert instance._bluez_hfp_conflict() is False
    assert len(background) == 1 and commands == []

    background[0]()

    assert instance._bluez_hfp_conflict() is True
    assert len(background) == 1 and commands == [False]


def test_hfp_conflict_is_not_reported_on_a_bluez_without_the_plugin(
    make_daemon, monkeypatch,
) -> None:
    instance, background, _commands = _conflict_daemon(
        make_daemon, monkeypatch, argv=["bluetoothd", "-E"], version="5.86",
    )
    instance._bluez_hfp_conflict()
    background[0]()

    assert instance._bluez_hfp_conflict() is False


def test_hfp_conflict_with_an_unknown_bluez_version_follows_the_options(
    make_daemon, monkeypatch,
) -> None:
    instance, background, _commands = _conflict_daemon(
        make_daemon, monkeypatch, argv=["bluetoothd", "-E"], version="",
    )
    instance._bluez_hfp_conflict()
    background[0]()

    assert instance._bluez_hfp_conflict() is True


def test_hfp_conflict_without_the_plugin_options_never_looks_up_the_version(
    make_daemon, monkeypatch,
) -> None:
    instance, background, _commands = _conflict_daemon(
        make_daemon, monkeypatch, argv=["bluetoothd", "-E", "-P", "hfp"], version="5.87",
    )

    assert instance._bluez_hfp_conflict() is False
    assert background == []
