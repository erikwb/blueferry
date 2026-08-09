"""The setup agent never authorizes an unrelated Bluetooth device."""

from __future__ import annotations

import threading

import dbus
import pytest

from blueferry import pairing_agent


def _agent(confirm):
    agent = pairing_agent.PairingAgent.__new__(pairing_agent.PairingAgent)
    agent._expected_device = "/org/bluez/hci0/dev_02_00_00_00_00_01"
    agent._confirmation = confirm
    agent._display = None
    return agent


def _wait_for(replies):
    for _ in range(200):
        if replies:
            return
        threading.Event().wait(0.01)


def test_numeric_comparison_requires_explicit_confirmation():
    requested = []
    replies = []
    agent = _agent(lambda passkey: requested.append(passkey) or True)

    agent.RequestConfirmation(
        dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_01"),
        dbus.UInt32(123456),
        lambda: replies.append("accepted"),
        lambda error: replies.append(error),
    )
    _wait_for(replies)

    assert requested == [123456]
    assert replies == ["accepted"]


def test_numeric_comparison_rejection_is_a_bluez_agent_error():
    agent = _agent(lambda _passkey: False)
    replies = []

    agent.RequestConfirmation(
        dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_01"),
        dbus.UInt32(123456),
        lambda: replies.append("accepted"),
        lambda error: replies.append(error),
    )
    _wait_for(replies)

    assert isinstance(replies[0], dbus.exceptions.DBusException)
    assert replies[0].get_dbus_name() == "org.bluez.Error.Rejected"


def test_confirmation_callback_failure_rejects_instead_of_hanging():
    def broken(_passkey):
        raise RuntimeError("client UI vanished")

    agent = _agent(broken)
    replies = []

    agent.RequestConfirmation(
        dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_01"),
        dbus.UInt32(123456),
        lambda: replies.append("accepted"),
        lambda error: replies.append(error),
    )
    _wait_for(replies)

    assert isinstance(replies[0], RuntimeError)


def test_agent_rejects_requests_for_any_other_device():
    agent = _agent(lambda _passkey: True)

    with pytest.raises(dbus.exceptions.DBusException) as raised:
        agent.RequestAuthorization(
            dbus.ObjectPath("/org/bluez/hci0/dev_02_00_00_00_00_02"),
            lambda: None,
            lambda _error: None,
        )

    assert raised.value.get_dbus_name() == "org.bluez.Error.Rejected"


def test_registered_agent_becomes_default_to_own_incoming_pairing():
    calls = []

    class Manager:
        def RegisterAgent(self, path, capability, **kwargs):
            calls.append(("register", str(path), capability, kwargs["timeout"]))

        def RequestDefaultAgent(self, path, **kwargs):
            calls.append(("default", str(path), kwargs["timeout"]))

    registered = pairing_agent.RegisteredPairingAgent.__new__(
        pairing_agent.RegisteredPairingAgent
    )
    registered._manager = Manager()
    registered._agent = object()
    registered._registered = False

    assert registered.__enter__() is registered
    assert calls == [
        ("register", pairing_agent.AGENT_PATH, "DisplayYesNo", 10.0),
        ("default", pairing_agent.AGENT_PATH, 10.0),
    ]


def test_registered_agent_unregisters_when_default_request_fails():
    calls = []

    class Manager:
        def RegisterAgent(self, path, _capability, **_kwargs):
            calls.append(("register", str(path)))

        def RequestDefaultAgent(self, _path, **_kwargs):
            raise dbus.exceptions.DBusException(
                "no", name="org.bluez.Error.DoesNotExist"
            )

        def UnregisterAgent(self, path, **_kwargs):
            calls.append(("unregister", str(path)))

    class Agent:
        def remove_from_connection(self):
            calls.append("removed")

    registered = pairing_agent.RegisteredPairingAgent.__new__(
        pairing_agent.RegisteredPairingAgent
    )
    registered._manager = Manager()
    registered._agent = Agent()
    registered._registered = False

    with pytest.raises(pairing_agent.PairingError):
        registered.__enter__()

    assert calls == [
        ("register", pairing_agent.AGENT_PATH),
        ("unregister", pairing_agent.AGENT_PATH),
        "removed",
    ]
    assert registered._registered is False
