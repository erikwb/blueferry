"""The saved phone-calls opt-in and switching the controller at runtime."""
from __future__ import annotations

import json

import pytest

from blueferry import config
from blueferry.calls.controller import CallController
from blueferry.calls.settings import CallsSettings, calls_enabled


def test_local_env_seeds_and_a_saved_choice_wins(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CALLS_ENABLED", True)
    assert CallsSettings(path).enabled is True

    CallsSettings(path).set(False)
    assert json.loads(path.read_text())["calls_enabled"] is False
    assert CallsSettings(path).enabled is False
    assert CallsSettings(path, default_enabled=True).enabled is False


def test_default_is_off_and_garbage_is_ignored(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "CALLS_ENABLED", False)
    assert CallsSettings(path).enabled is False
    path.write_text('{"calls_enabled": "yes"}')
    path.chmod(0o600)
    assert CallsSettings(path).enabled is False
    with pytest.raises(ValueError):
        CallsSettings(path).set("yes")  # type: ignore[arg-type]


def test_saving_keeps_other_settings(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"proximity_lock_enabled": true}')
    path.chmod(0o600)

    CallsSettings(path).set(True)

    assert json.loads(path.read_text()) == {"proximity_lock_enabled": True, "calls_enabled": True}


def test_calls_enabled_reads_the_configured_document(isolated_state, monkeypatch) -> None:
    monkeypatch.setattr(config, "CALLS_ENABLED", False)
    assert calls_enabled() is False
    CallsSettings().set(True)
    assert calls_enabled() is True


def test_switching_on_starts_discovery_and_off_releases_everything() -> None:
    from tests.test_calls_controller import FakeTransport, Timers, _modem

    transport = FakeTransport()
    timers = Timers()
    events = []
    states = []
    controller = CallController(
        enabled=False, mac="AA:BB:CC:DD:EE:FF", adapter="hci0", transport=transport,
        on_event=events.append, on_state_changed=lambda: states.append(controller.state),
        schedule=timers.schedule, cancel=timers.cancel,
    )
    controller.start()
    assert transport.pending == []

    controller.set_enabled(True)
    assert controller.enabled is True
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()  # BlueFerry powered the modem

    controller.set_enabled(False)

    assert controller.enabled is False and controller.state == "disabled"
    assert states[-1] == "disabled"
    assert all(match.removed for match in transport.matches)
    assert timers.entries == {}
    assert transport.sent and transport.sent[-1][3][0] == "Powered"
    controller.set_enabled(False)  # idempotent


def test_switching_off_during_a_call_closes_its_desktop_view() -> None:
    import dbus

    from tests.test_calls_controller import CALL, MODEM, VOICE_CALL_MANAGER_IFACE, _ready

    controller, transport, _timers, _changes, events = _ready()
    transport.emit(
        VOICE_CALL_MANAGER_IFACE, "CallAdded", MODEM, dbus.ObjectPath(CALL),
        dbus.Dictionary({"State": dbus.String("incoming")}, signature="sv"),
    )

    controller.set_enabled(False)

    assert [event.kind for event in events] == ["call_incoming", "call_ended"]
    assert controller.calls() == []
