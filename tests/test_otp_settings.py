"""The saved one-time code opt-in and switching the sink at runtime."""
from __future__ import annotations

import itertools
import json
import os

import dbus
import dbus.service
import pytest

from blueferry import config, event_dispatcher
from blueferry.backend_operations import BackendDependencies
from blueferry.dbus_service import MessagesService
from blueferry.otp_settings import OtpSettings
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE
from tests.test_calls_dbus import _call, _Sessions
from tests.test_otp_sink import _Bus, _dispatcher, _sink, _SqliteSink, _Writer

_service_ids = itertools.count()


def test_local_env_seeds_and_a_saved_choice_wins(tmp_path, monkeypatch, caplog) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "OTP_AUTOCOPY", True)
    assert OtpSettings(path).enabled is True

    OtpSettings(path).set(False)
    assert json.loads(path.read_text())["otp_autocopy"] is False

    monkeypatch.setenv("BLUEFERRY_OTP_AUTOCOPY", "true")
    caplog.set_level("INFO", logger="blueferry.otp_settings")
    assert OtpSettings(path, log_overrides=True).enabled is False
    assert "BLUEFERRY_OTP_AUTOCOPY is ignored" in caplog.text


def test_default_is_off_and_garbage_is_ignored(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "OTP_AUTOCOPY", False)
    assert OtpSettings(path).enabled is False
    path.write_text('{"otp_autocopy": "yes"}')
    path.chmod(0o600)
    assert OtpSettings(path).enabled is False
    with pytest.raises(ValueError):
        OtpSettings(path).set("yes")  # type: ignore[arg-type]


def test_saving_keeps_other_settings(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"calls_enabled": true}')
    path.chmod(0o600)

    OtpSettings(path).set(True)

    assert json.loads(path.read_text()) == {"calls_enabled": True, "otp_autocopy": True}


def test_switching_at_runtime_adds_and_closes_the_sink(monkeypatch) -> None:
    enabled = [False]
    writer = _Writer()
    sink, _notifier, _timers = _sink(writer)
    built = []

    def factory(**_kwargs):
        built.append(sink)
        return sink

    dispatcher = _dispatcher(monkeypatch, enabled=False, factory=factory)
    dispatcher._otp_autocopy = lambda: enabled[0]
    dispatcher.setup()
    assert "otp-clipboard" not in dispatcher.names

    enabled[0] = True
    dispatcher.apply_otp_autocopy()
    dispatcher.apply_otp_autocopy()
    assert dispatcher.names.count("otp-clipboard") == 1

    enabled[0] = False
    dispatcher.apply_otp_autocopy()
    assert "otp-clipboard" not in dispatcher.names
    # Closing releases the clipboard helper, so nothing stays behind.
    assert writer.released is True
    assert built == [sink]


def test_applying_before_setup_builds_nothing(monkeypatch) -> None:
    built = []
    dispatcher = _dispatcher(
        monkeypatch, enabled=True, factory=lambda **kwargs: built.append(kwargs),
    )
    dispatcher.apply_otp_autocopy()
    assert built == []


def test_daemon_saves_and_applies_the_opt_in(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(config, "OTP_AUTOCOPY", False)
    daemon = make_daemon()
    monkeypatch.setattr(event_dispatcher, "SqliteSink", _SqliteSink)
    sink, _notifier, _timers = _sink(_Writer())
    daemon.events._session_bus = _Bus()
    daemon.events._notification_sink_factory = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("no notification server")
    )
    daemon.events._schedule = lambda *_args: 1
    daemon.events._otp_sink_factory = lambda **_kwargs: sink
    statuses = []
    monkeypatch.setattr(daemon, "_emit_status", lambda: statuses.append(True))
    daemon.events.setup()
    assert daemon._status()["otp_autocopy"] is False

    assert daemon._set_otp_autocopy(True) == {"otp_autocopy": True}
    assert "otp-clipboard" in daemon.events.names
    assert json.loads(config.SETTINGS_JSON.read_text())["otp_autocopy"] is True
    assert daemon._status()["otp_autocopy"] is True

    assert daemon._set_otp_autocopy(False) == {"otp_autocopy": False}
    assert "otp-clipboard" not in daemon.events.names
    assert statuses == [True, True]


@pytest.mark.private_dbus
def test_set_otp_autocopy_round_trips_over_the_bus() -> None:
    toggles = []

    def configure(enabled):
        toggles.append(enabled)
        return {"otp_autocopy": enabled}

    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Otpp{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False},
            set_otp_autocopy=configure,
        ),
    )
    try:
        enabled = json.loads(_call(name, MESSAGES_IFACE, "SetOtpAutocopy", True)["value"])
        disabled = json.loads(_call(name, MESSAGES_IFACE, "SetOtpAutocopy", False)["value"])
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert toggles == [True, False]
    assert enabled == {"otp_autocopy": True}
    assert disabled == {"otp_autocopy": False}


@pytest.mark.private_dbus
def test_set_otp_autocopy_without_a_daemon_hook_is_not_ready() -> None:
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Otpn{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(status_provider=lambda: {"initializing": False}),
    )
    try:
        outcome = _call(name, MESSAGES_IFACE, "SetOtpAutocopy", True)
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert outcome["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"
