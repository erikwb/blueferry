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

from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.calls.phone_status import (
    HANDSFREE_IFACE,
    NETWORK_REGISTRATION_IFACE,
    PHONE_STATUS_KEYS,
    PhoneStatus,
    apply_properties,
    parse_battery_charge,
    parse_network_name,
    parse_network_status,
    parse_signal_strength,
)
from blueferry.models import BackendStatus, phone_status_fields

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


# ---- backend and client model ------------------------------------------------


def test_disabled_backend_status_reports_unknown_phone_values() -> None:
    sessions = SimpleNamespace(map=None, pbap=None, map_path="", report_error=lambda _e: None)

    status = BackendOperations(sessions, BackendDependencies()).status()

    assert {key: status[key] for key in PHONE_STATUS_KEYS} == dict.fromkeys(PHONE_STATUS_KEYS)


def test_status_model_decodes_phone_fields_defensively() -> None:
    status = BackendStatus.from_dict({
        "phone_battery_level": 60, "phone_signal_strength": 80,
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
