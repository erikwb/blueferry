"""The setup agent never authorizes an unrelated Bluetooth device."""

from __future__ import annotations

import logging
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


def test_registered_agent_is_default_only_for_context_lifetime(caplog):
    calls = []
    caplog.set_level(logging.DEBUG, logger="blueferry.pairing_agent")

    class Manager:
        def RegisterAgent(self, path, capability, **kwargs):
            calls.append(("register", str(path), capability, kwargs["timeout"]))

        def RequestDefaultAgent(self, path, **kwargs):
            calls.append(("default", str(path), kwargs["timeout"]))

        def UnregisterAgent(self, path, **kwargs):
            calls.append(("unregister", str(path), kwargs["timeout"]))

    class Agent:
        def remove_from_connection(self):
            calls.append("removed")

    registered = pairing_agent.RegisteredPairingAgent.__new__(
        pairing_agent.RegisteredPairingAgent
    )
    registered._manager = Manager()
    registered._agent = Agent()
    registered._registered = False
    registered._expected_device = "/device"
    registered._make_default = True

    with registered:
        calls.append("pairing")

    assert calls == [
        ("register", pairing_agent.AGENT_PATH, "DisplayYesNo", 10.0),
        ("default", pairing_agent.AGENT_PATH, 10.0),
        "pairing",
        ("unregister", pairing_agent.AGENT_PATH, 10.0),
        "removed",
    ]
    assert registered._registered is False
    assert "registering device-scoped pairing agent" in caplog.text
    assert "pairing agent is now the BlueZ default" in caplog.text
    assert "restoring previous default" in caplog.text


def test_registered_agent_cleans_up_when_default_request_fails():
    calls = []

    class Manager:
        def RegisterAgent(self, path, _capability, **_kwargs):
            calls.append(("register", str(path)))

        def RequestDefaultAgent(self, path, **_kwargs):
            calls.append(("default", str(path)))
            raise dbus.exceptions.DBusException(
                "not authorized", name="org.bluez.Error.Rejected"
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
    registered._expected_device = "/device"
    registered._make_default = True

    with pytest.raises(pairing_agent.PairingError, match="not authorized"):
        registered.__enter__()

    assert calls == [
        ("register", pairing_agent.AGENT_PATH),
        ("default", pairing_agent.AGENT_PATH),
        ("unregister", pairing_agent.AGENT_PATH),
        "removed",
    ]
    assert registered._registered is False


def test_registered_agent_cleans_up_when_registration_fails():
    calls = []

    class Manager:
        def RegisterAgent(self, path, _capability, **_kwargs):
            calls.append(("register", str(path)))
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
    registered._expected_device = "/device"

    with pytest.raises(pairing_agent.PairingError):
        registered.__enter__()

    assert calls == [
        ("register", pairing_agent.AGENT_PATH),
        "removed",
    ]
    assert registered._registered is False


def test_registered_agent_does_not_replace_default_for_explicit_pairing():
    calls = []

    class Manager:
        def RegisterAgent(self, path, capability, **_kwargs):
            calls.append(("register", str(path), capability))

        def RequestDefaultAgent(self, _path, **_kwargs):
            calls.append("unexpected-default")

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
    registered._expected_device = "/device"
    registered._make_default = False

    with registered:
        calls.append("pairing")

    assert calls == [
        ("register", pairing_agent.AGENT_PATH, "DisplayYesNo"),
        "pairing",
        ("unregister", pairing_agent.AGENT_PATH),
        "removed",
    ]


def test_registered_agent_pairs_asynchronously_on_its_own_bus(monkeypatch):
    calls = []

    class Loop:
        def run(self):
            calls.append("loop-run")

        def quit(self):
            calls.append("loop-quit")

    class Device:
        def Pair(self, **kwargs):
            calls.append(("pair", kwargs["timeout"]))
            kwargs["reply_handler"]()

    class Bus:
        def get_object(self, service, path):
            calls.append(("object", service, path))
            return object()

    monkeypatch.setattr(pairing_agent.GLib, "MainLoop", Loop)
    monkeypatch.setattr(pairing_agent.GLib, "timeout_add", lambda *_args: 7)
    monkeypatch.setattr(
        pairing_agent.GLib,
        "source_remove",
        lambda source: calls.append(("source-remove", source)),
    )
    monkeypatch.setattr(pairing_agent.dbus, "Interface", lambda *_args: Device())
    registered = pairing_agent.RegisteredPairingAgent.__new__(
        pairing_agent.RegisteredPairingAgent
    )
    registered._bus = Bus()
    registered._expected_device = "/device"

    registered.pair(timeout=42.0)

    assert calls == [
        ("object", "org.bluez", "/device"),
        ("pair", 42.0),
        "loop-quit",
        "loop-run",
        ("source-remove", 7),
    ]


def test_registered_agent_propagates_async_pairing_failure(monkeypatch):
    error = dbus.exceptions.DBusException(
        "rejected", name="org.bluez.Error.AuthenticationRejected"
    )

    class Loop:
        def run(self):
            pass

        def quit(self):
            pass

    class Device:
        def Pair(self, **kwargs):
            kwargs["error_handler"](error)

    monkeypatch.setattr(pairing_agent.GLib, "MainLoop", Loop)
    monkeypatch.setattr(pairing_agent.GLib, "timeout_add", lambda *_args: 7)
    monkeypatch.setattr(pairing_agent.GLib, "source_remove", lambda _source: None)
    monkeypatch.setattr(pairing_agent.dbus, "Interface", lambda *_args: Device())
    registered = pairing_agent.RegisteredPairingAgent.__new__(
        pairing_agent.RegisteredPairingAgent
    )
    registered._bus = type("Bus", (), {"get_object": lambda *_args: object()})()
    registered._expected_device = "/device"

    with pytest.raises(dbus.exceptions.DBusException) as raised:
        registered.pair()

    assert raised.value is error
