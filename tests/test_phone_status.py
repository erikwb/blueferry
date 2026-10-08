"""iPhone battery/signal/operator from oFono: parsing, warnings, and wiring.

Pure logic plus fakes only; nothing here reaches oFono, a bus, or a phone.
The controller's use of these parsers is covered in test_calls_controller.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import dbus
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from blueferry import cli_calls, config
from blueferry.calls.phone_status import (
    HANDSFREE_IFACE,
    NETWORK_REGISTRATION_IFACE,
    PHONE_STATUS_KEYS,
    LowBatteryMonitor,
    PhoneStatus,
    apply_properties,
    parse_battery_charge,
    parse_network_name,
    parse_network_status,
    parse_signal_strength,
)
from blueferry.cli import app
from blueferry.event_dispatcher import EventDispatcher
from blueferry.models import BackendStatus, phone_status_fields
from blueferry.sinks.libnotify import LibnotifySink

PROPERTY_SETTINGS = settings(max_examples=150, derandomize=True, deadline=None)


# ---- parsing ---------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (dbus.Byte(0), 0), (dbus.Byte(5), 5), (3, 3),
    (dbus.Byte(6), None), (-1, None), (dbus.Boolean(True), None), (True, None),
    ("3", None), (3.0, None), (None, None),
])
def test_battery_charge_accepts_only_the_hfp_range(value, expected) -> None:
    assert parse_battery_charge(value) == expected


@pytest.mark.parametrize("value,expected", [
    (dbus.Byte(0), 0), (dbus.Byte(100), 100), (dbus.Byte(60), 60),
    (dbus.Byte(101), None), (dbus.Boolean(False), None), ("80", None),
])
def test_signal_strength_is_a_percentage(value, expected) -> None:
    assert parse_signal_strength(value) == expected


def test_network_status_and_name_are_bounded_display_values() -> None:
    assert parse_network_status(dbus.String("roaming")) == "roaming"
    assert parse_network_status("sideways") == "unknown"
    assert parse_network_status(7) is None
    assert parse_network_name("") is None
    assert parse_network_name(5) is None
    assert parse_network_name("Sun\x1b[31mrise\n‮") == "Sun [31mrise"
    assert len(parse_network_name("x" * 500) or "") == 64


@PROPERTY_SETTINGS
@given(
    interface=st.sampled_from([HANDSFREE_IFACE, NETWORK_REGISTRATION_IFACE, "org.ofono.Other"]),
    properties=st.one_of(
        st.none(),
        st.integers(),
        st.dictionaries(
            st.sampled_from([
                "BatteryChargeLevel", "Strength", "Name", "Status", "Features", "Other",
            ]),
            st.one_of(
                st.none(), st.booleans(), st.integers(), st.text(max_size=300),
                st.lists(st.text(max_size=8), max_size=3), st.floats(allow_nan=True),
            ),
            max_size=8,
        ),
    ),
)
def test_arbitrary_properties_never_crash_and_stay_in_range(interface, properties) -> None:
    status = apply_properties(PhoneStatus(), interface, properties).to_status()

    assert set(status) == set(PHONE_STATUS_KEYS)
    battery = status["phone_battery_level"]
    signal = status["phone_signal_strength"]
    assert battery is None or battery in {0, 20, 40, 60, 80, 100}
    assert signal is None or (isinstance(signal, int) and 0 <= signal <= 100)
    name = status["phone_network_name"]
    assert name is None or (isinstance(name, str) and 0 < len(name) <= 64 and "\n" not in name)
    json.dumps(status)


def test_get_properties_replaces_only_its_interface() -> None:
    status = PhoneStatus(battery_steps=2, signal_strength=40,
                         network_name="Old", network_status="registered")

    refreshed = apply_properties(status, NETWORK_REGISTRATION_IFACE, {"Status": "roaming"})

    assert refreshed.battery_steps == 2
    assert refreshed.network_name is None and refreshed.signal_strength is None
    assert refreshed.to_status()["phone_network_status"] == "roaming"
    # A malformed reply keeps the previous values instead of wiping them.
    assert apply_properties(status, HANDSFREE_IFACE, [1, 2]) == status


def test_losing_registration_forgets_the_signal_strength() -> None:
    status = PhoneStatus(signal_strength=80, network_name="Sunrise", network_status="registered")

    status = status.with_network("Status", "searching")
    assert status.signal_strength is None
    # Registration returns before oFono has sent a fresh Strength.
    status = status.with_network("Status", "registered")
    assert status.to_status()["phone_signal_strength"] is None
    assert status.with_network("Strength", 40).to_status()["phone_signal_strength"] == 40
    # Moving between registered states keeps the known strength.
    assert status.with_network("Strength", 40).with_network(
        "Status", "roaming",
    ).signal_strength == 40


# ---- low-battery warning -------------------------------------------------------


def test_low_battery_warns_once_per_discharge_cycle() -> None:
    monitor = LowBatteryMonitor(20)

    observed = [monitor.observe(level) for level in (100, 60, 40, 20, 20, 0, 20, 0)]
    assert observed == [False, False, False, True, False, False, False, False]

    # Unknown (disconnect, oFono restart) neither warns nor re-arms.
    assert monitor.observe(None) is False
    assert monitor.observe(20) is False
    # Charging one HFP step above the threshold re-arms the next cycle.
    assert monitor.observe(40) is False
    assert monitor.observe(20) is True


def test_low_battery_threshold_is_clamped_and_first_reading_can_warn() -> None:
    assert LowBatteryMonitor(250).threshold == 80
    assert LowBatteryMonitor(100).threshold == 80
    assert LowBatteryMonitor(-5).threshold == 0
    monitor = LowBatteryMonitor(20)
    assert monitor.observe(0) is True
    assert monitor.warned
    # The highest threshold can still re-arm: 100 % is one step above 80 %.
    top = LowBatteryMonitor(100)
    assert [top.observe(level) for level in (80, 100, 80)] == [True, False, True]


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("true", True), ("1", True),
])
def test_battery_warning_is_strictly_opt_in(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("BLUEFERRY_PHONE_BATTERY_NOTIFY", raising=False)
    else:
        monkeypatch.setenv("BLUEFERRY_PHONE_BATTERY_NOTIFY", value)
    assert config._env_opt_in("BLUEFERRY_PHONE_BATTERY_NOTIFY") is expected


def test_battery_keys_are_read_from_local_env(tmp_path) -> None:
    path = tmp_path / "local.env"
    path.write_text(
        "BLUEFERRY_PHONE_BATTERY_NOTIFY=true\nBLUEFERRY_PHONE_BATTERY_LOW_PERCENT=40\n"
    )
    path.chmod(0o600)

    assert config.read_local_env(path) == {
        "BLUEFERRY_PHONE_BATTERY_NOTIFY": "true",
        "BLUEFERRY_PHONE_BATTERY_LOW_PERCENT": "40",
    }


# ---- daemon wiring -------------------------------------------------------------


class _Timers:
    def __init__(self) -> None:
        self.entries: list[tuple[int, object]] = []

    def schedule(self, delay, callback) -> int:
        self.entries.append((delay, callback))
        return len(self.entries)

    def fire(self) -> None:
        entries, self.entries = self.entries, []
        for _delay, callback in entries:
            callback()


def _daemon_with_recorders(make_daemon, *, warning=False):
    instance = make_daemon()
    seen: list[object] = []
    now = [1000.0]
    timers = _Timers()
    instance._emit_status = lambda: seen.append("status")
    instance._idle_add = lambda callback, **_options: callback()
    instance._clock = lambda: now[0]
    instance._schedule_seconds = timers.schedule
    instance.battery_warning._enabled = warning
    instance.events.phone_battery_low = (
        lambda percent, exact=False: seen.append(("low", percent, exact))
    )
    return instance, seen, now, timers


def _hfp(instance, steps):
    instance.calls.enabled = True
    instance.calls._phone = PhoneStatus(battery_steps=steps)
    instance._phone_status_changed()


def test_phone_status_changes_emit_status_but_warn_only_when_opted_in(make_daemon) -> None:
    instance, seen, now, _timers = _daemon_with_recorders(make_daemon)
    _hfp(instance, 0)
    assert seen == ["status"]

    instance, seen, now, _timers = _daemon_with_recorders(make_daemon, warning=True)
    for steps in (3, 1, 1, None, 1, 0, 3, 1):
        now[0] += 60
        _hfp(instance, steps)

    # Unchanged values publish nothing.
    assert seen.count("status") == 7
    assert [item for item in seen if item != "status"] == [("low", 20, False), ("low", 20, False)]


def test_phone_status_is_published_at_most_every_few_seconds(make_daemon) -> None:
    from blueferry import daemon as daemon_mod

    instance, seen, now, timers = _daemon_with_recorders(make_daemon)
    _hfp(instance, 3)
    _hfp(instance, 2)
    _hfp(instance, 1)
    assert seen == ["status"]
    assert [delay for delay, _ in timers.entries] == [daemon_mod.PHONE_STATUS_MIN_INTERVAL_SEC]

    now[0] += daemon_mod.PHONE_STATUS_MIN_INTERVAL_SEC
    timers.fire()
    assert seen == ["status", "status"]
    assert instance._published_phone["phone_battery_level"] == 20

    # A burst that ends where it started publishes nothing.
    _hfp(instance, 2)
    _hfp(instance, 1)
    now[0] += daemon_mod.PHONE_STATUS_MIN_INTERVAL_SEC
    timers.fire()
    assert seen == ["status", "status"]


def test_le_battery_wins_over_hfp_and_needs_a_connected_phone(make_daemon) -> None:
    from types import SimpleNamespace

    instance, _seen, _now, _timers = _daemon_with_recorders(make_daemon)
    instance.phone_battery._gatt = 87
    instance.calls.enabled = True
    instance.calls._phone = PhoneStatus(battery_steps=4, network_status="registered",
                                        signal_strength=60)

    instance.bearers = SimpleNamespace(bredr_connected=False, le_connected=False)
    away = instance._phone_status()
    assert away["phone_battery_level"] == 80 and away["phone_battery_source"] == "hfp"

    instance.bearers = SimpleNamespace(bredr_connected=False, le_connected=True)
    here = instance._phone_status()
    assert here["phone_battery_level"] == 87 and here["phone_battery_source"] == "gatt"
    assert here["phone_signal_strength"] == 60

    instance.calls.enabled = False
    off = instance._phone_status()
    assert off["phone_battery_level"] == 87 and off["phone_signal_strength"] is None


def test_exact_le_battery_warns_without_the_step_note(make_daemon) -> None:
    from types import SimpleNamespace

    instance, seen, _now, _timers = _daemon_with_recorders(make_daemon, warning=True)
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=True)
    instance.phone_battery._gatt = 12
    instance._phone_status_changed()

    assert ("low", 12, True) in seen


def test_a_lost_le_level_does_not_warn_from_the_hfp_step(make_daemon) -> None:
    from types import SimpleNamespace

    instance, seen, now, _timers = _daemon_with_recorders(make_daemon, warning=True)
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=True)
    instance.calls.enabled = True
    instance.calls._phone = PhoneStatus(battery_steps=1)  # 20 %, a step
    instance.phone_battery._gatt = 23
    instance._phone_status_changed()

    # LE drops out while Classic and HFP stay: the 20 % step must not warn.
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=False)
    instance.phone_battery._gatt = None
    now[0] += 60
    instance._phone_status_changed()
    assert not [item for item in seen if item != "status"]

    # The exact level coming back below the threshold still warns.
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=True)
    instance.phone_battery._gatt = 18
    now[0] += 60
    instance._phone_status_changed()
    assert ("low", 18, True) in seen


def test_hfp_steps_warn_again_after_the_phone_was_away(make_daemon) -> None:
    from types import SimpleNamespace

    instance, seen, now, _timers = _daemon_with_recorders(make_daemon, warning=True)
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=True)
    instance.phone_battery._gatt = 60
    instance._phone_status_changed()
    instance.bearers = SimpleNamespace(bredr_connected=False, le_connected=False)
    instance._phone_status_changed()

    # A later connection without any LE level falls back to the HFP steps.
    instance.bearers = SimpleNamespace(bredr_connected=True, le_connected=False)
    instance.phone_battery._gatt = None
    now[0] += 60
    _hfp(instance, 1)
    assert ("low", 20, False) in seen


def test_battery_warning_setting_is_saved(make_daemon) -> None:
    from blueferry.phone_battery import BatteryWarningSettings

    instance, seen, _now, _timers = _daemon_with_recorders(make_daemon)
    assert instance._set_battery_warning(True)["phone_battery_warning"] is True
    assert BatteryWarningSettings().enabled is True
    assert "status" in seen


def test_daemon_wires_the_controller_and_battery_to_its_handler(make_daemon) -> None:
    instance, _seen, _now, _timers = _daemon_with_recorders(make_daemon)
    instance.calls._on_phone_status(PhoneStatus(battery_steps=1))
    instance.phone_battery._on_change()

    assert instance.low_battery.threshold == config.PHONE_BATTERY_LOW_PERCENT
    assert instance.phone_battery.device_path.endswith(config.IPHONE_MAC.replace(":", "_"))


# ---- desktop warning -------------------------------------------------------------


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def Notify(self, *args):
        self.calls.append(args)
        return 7


def _sink(policy="messages"):
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: policy
    sink._notif = _FakeNotifications()
    return sink


def test_libnotify_battery_warning_respects_the_notification_policy() -> None:
    sink = _sink()
    sink.handle_phone_battery_low(20)

    notify = sink._notif.calls[0]
    assert "battery low" in notify[3]
    assert "About 20 %" in notify[4]
    assert list(notify[5]) == []

    exact = _sink()
    exact.handle_phone_battery_low(12, exact=True)
    assert exact._notif.calls[0][4] == "12 % left."

    silent = _sink("none")
    silent.handle_phone_battery_low(0)
    assert silent._notif.calls == []


def test_dispatcher_routes_the_warning_to_sinks_that_opt_in() -> None:
    dispatcher = EventDispatcher.__new__(EventDispatcher)
    received = []

    class Broken:
        name = "broken"

        def handle_phone_battery_low(self, _percent, exact=False):
            raise RuntimeError("sink bug")

    dispatcher.sinks = [
        SimpleNamespace(name="plain"),
        Broken(),
        SimpleNamespace(
            name="ok", handle_phone_battery_low=lambda p, exact: received.append((p, exact)),
        ),
    ]

    dispatcher.phone_battery_low(20)
    dispatcher.phone_battery_low(12, exact=True)

    assert received == [(20, False), (12, True)]


# ---- backend and client model ------------------------------------------------


def test_daemon_status_reports_unknown_phone_values_by_default(make_daemon) -> None:
    status = make_daemon()._status()

    assert {key: status[key] for key in PHONE_STATUS_KEYS} == dict.fromkeys(PHONE_STATUS_KEYS)
    assert status["phone_battery_warning"] is False


def test_status_model_decodes_phone_fields_defensively() -> None:
    status = BackendStatus.from_dict({
        "phone_battery_level": 60, "phone_battery_source": "hfp", "phone_signal_strength": 80,
        "phone_network_name": "Sunrise", "phone_network_status": "roaming",
    })
    assert (status.phone_battery_level, status.phone_signal_strength) == (60, 80)
    assert status.to_dict()["phone_network_name"] == "Sunrise"
    assert phone_status_fields(status) == [
        ("Battery", "about 60 %"), ("Signal", "80 %"), ("Network", "Sunrise (roaming)"),
    ]

    malformed = BackendStatus.from_dict({
        "phone_battery_level": True, "phone_signal_strength": 400,
        "phone_network_name": 5, "phone_network_status": "",
    })
    assert malformed.phone_battery_level is None
    assert malformed.phone_signal_strength is None
    assert malformed.phone_network_name is None
    assert phone_status_fields(malformed) == []
    assert BackendStatus.from_dict({}).phone_battery_level is None


# ---- CLI ---------------------------------------------------------------------------


def test_phone_status_fields_skip_unknown_registration_and_optional_network() -> None:
    unknown = BackendStatus.from_dict({"phone_network_status": "unknown"})
    assert phone_status_fields(unknown) == []
    named_unknown = BackendStatus.from_dict({
        "phone_network_name": "Sunrise", "phone_network_status": "unknown",
    })
    assert phone_status_fields(named_unknown) == [("Network", "Sunrise")]
    searching = BackendStatus.from_dict({"phone_network_status": "searching"})
    assert phone_status_fields(searching) == [("Network", "searching")]

    full = BackendStatus.from_dict({
        "phone_battery_level": 20, "phone_battery_source": "hfp", "phone_signal_strength": 40,
        "phone_network_name": "Sunrise", "phone_network_status": "registered",
    })
    assert phone_status_fields(full, include_network=False) == [
        ("Battery", "about 20 %"), ("Signal", "40 %"),
    ]
    exact = BackendStatus.from_dict({"phone_battery_level": 87, "phone_battery_source": "gatt"})
    assert phone_status_fields(exact) == [("Battery", "87 %")]


def test_phone_status_labels_and_values_are_translatable(monkeypatch) -> None:
    from blueferry import models
    from blueferry.ui import status_presenter

    translations = {
        "Battery": "Akku", "Signal": "Signal", "Network": "Netz",
        "about {percent} %": "etwa {percent} %", "{percent} %": "{percent} %",
        "{label} {value}": "{label}: {value}",
    }
    monkeypatch.setattr(models, "_", lambda text: translations.get(text, text))
    monkeypatch.setattr(status_presenter, "_", lambda text: translations.get(text, text))
    status = BackendStatus.from_dict({
        "phone_battery_level": 60, "phone_battery_source": "hfp", "phone_signal_strength": 80,
        "phone_network_name": "Sunrise", "phone_network_status": "registered",
    })

    assert phone_status_fields(status) == [
        ("Akku", "etwa 60 %"), ("Signal", "80 %"), ("Netz", "Sunrise"),
    ]
    assert status_presenter.connection_subtitle(
        {"connectivity_state": "ready", "phone_battery_level": 60, "phone_battery_source": "hfp"},
        reachable=True,
    ) == "Ready · Akku: etwa 60 %"


class _StatusClient:
    def __init__(self, **status) -> None:
        self._status = BackendStatus.from_dict(status)

    def status(self):
        return self._status


def _invoke(monkeypatch, client, *args):
    monkeypatch.setattr(cli_calls, "_client", lambda: client)
    return CliRunner().invoke(app, ["phone-status", *args])


def test_cli_phone_status_prints_known_values(monkeypatch) -> None:
    result = _invoke(monkeypatch, _StatusClient(
        calls_enabled=True, calls_state="ready",
        phone_battery_level=40, phone_battery_source="hfp", phone_signal_strength=60,
        phone_network_name="Sun\x1b[2Jrise", phone_network_status="registered",
    ))

    assert result.exit_code == 0, result.output
    assert "Battery: about 40 %" in result.output
    assert "Signal:  60 %" in result.output
    assert "Network:" in result.output and "\x1b" not in result.output
    assert "20 % steps" in result.output


def test_cli_phone_status_explains_unknown_values_and_calls(monkeypatch) -> None:
    unknown = _invoke(monkeypatch, _StatusClient())
    assert unknown.exit_code == 0
    assert "Phone status unknown" in unknown.output
    assert "blueferry calls enable" in unknown.output

    le_only = _invoke(monkeypatch, _StatusClient(
        phone_battery_level=87, phone_battery_source="gatt", phone_battery_warning=True,
    ))
    assert "Battery: 87 %" in le_only.output and "20 % steps" not in le_only.output
    assert "Low-battery warning: on." in le_only.output


def test_cli_phone_status_json_has_exactly_the_phone_keys(monkeypatch) -> None:
    result = _invoke(monkeypatch, _StatusClient(phone_battery_level=100), "--json")

    assert json.loads(result.output) == {
        "phone_battery_level": 100, "phone_battery_source": None,
        "phone_signal_strength": None, "phone_network_name": None,
        "phone_network_status": None,
    }


def test_cli_phone_status_saves_the_warning(monkeypatch) -> None:
    saved = []

    class Client(_StatusClient):
        def set_phone_battery_warning(self, enabled):
            saved.append(enabled)
            return enabled

    on = _invoke(monkeypatch, Client(), "--warn")
    off = _invoke(monkeypatch, Client(), "--no-warn")

    assert saved == [True, False]
    assert "Low-battery warning on." in on.output and "off." in off.output


def test_cli_phone_status_reports_backend_errors(monkeypatch) -> None:
    from blueferry.client import BackendError

    class Failing:
        def status(self):
            raise BackendError("daemon is not running")

    result = _invoke(monkeypatch, Failing())
    assert result.exit_code == 3
    assert "Could not read status" in result.output
