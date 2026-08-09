"""The setup agent never authorizes an unrelated Bluetooth device."""

from __future__ import annotations

import dbus
import pytest

from blueferry import pairing_agent


def _agent(confirm):
    agent = pairing_agent.PairingAgent.__new__(pairing_agent.PairingAgent)
    agent._expected_device = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    agent._confirmation = confirm
    agent._display = None
    return agent


def test_numeric_comparison_requires_explicit_confirmation():
    requested = []
    agent = _agent(lambda passkey: requested.append(passkey) or True)

    agent.RequestConfirmation(
        dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_01"),
        dbus.UInt32(123456),
    )

    assert requested == [123456]


def test_numeric_comparison_rejection_is_a_bluez_agent_error():
    agent = _agent(lambda _passkey: False)

    with pytest.raises(dbus.exceptions.DBusException) as raised:
        agent.RequestConfirmation(
            dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_01"),
            dbus.UInt32(123456),
        )

    assert raised.value.get_dbus_name() == "org.bluez.Error.Rejected"


def test_agent_rejects_requests_for_any_other_device():
    agent = _agent(lambda _passkey: True)

    with pytest.raises(dbus.exceptions.DBusException) as raised:
        agent.RequestAuthorization(
            dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_02")
        )

    assert raised.value.get_dbus_name() == "org.bluez.Error.Rejected"
