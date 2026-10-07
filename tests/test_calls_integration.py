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
    assert instance.calls.snapshot()["calls_state"] == "disabled"
    status = BackendOperations(
        _Sessions(), BackendDependencies(calls=instance.calls),
    ).status()
    assert status["calls_enabled"] is False
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
