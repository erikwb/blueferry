"""Every exported object checks argument types on the isolated test bus.

The objects get inert collaborators: nothing here reaches BlueZ, a phone, or
the installed daemon. Calls are raw messages, so the test controls the wire
signature instead of letting dbus-python pick one from the argument values.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import dbus
import dbus.lowlevel
import dbus.mainloop
import dbus.service
import pytest
from gi.repository import GLib

from blueferry import bluez_setup, glib_client_activation, pairing_agent
from blueferry.client_activation import ACTIVATION_INTERFACE, ACTIVATION_PATH
from blueferry.dbus_service import MessagesService
from blueferry.mpris import MPRIS_PATH, PLAYER_IFACE, MprisPlayer
from blueferry.protocol import MESSAGES_IFACE, OBJECT_PATH
from tests.private_bus import open_private_bus

pytestmark = pytest.mark.private_dbus

DEVICE = "/org/bluez/hci7/dev_00_11_22_33_44_55"
ADVERT_PATH = "/io/weirdware/BlueFerry/test/advert"


def _messages(connection, effects, _monkeypatch):
    operations = SimpleNamespace(
        clear_history=effects.append, dependencies=SimpleNamespace(storage=None),
    )
    guard = SimpleNamespace(authorize=lambda _sender, _action: None, close=lambda: None)
    service = MessagesService(
        dbus.service.BusName("io.weirdware.BlueFerry.StrictTest", bus=connection),
        SimpleNamespace(), operations=operations, caller_guard=guard,
    )
    return service.close, (OBJECT_PATH, MESSAGES_IFACE, "ClearHistory")


def _agent(connection, effects, _monkeypatch):
    pairing_agent.PairingAgent(connection, DEVICE, lambda _passkey: False, effects.append)
    return None, (pairing_agent.AGENT_PATH, pairing_agent.AGENT_INTERFACE, "DisplayPasskey")


def _advert(connection, _effects, monkeypatch):
    # The advert pins its manager proxy to the owner of org.bluez.
    connection.request_name("org.bluez", dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    monkeypatch.setattr(bluez_setup, "get_system_bus", lambda: connection)
    bluez_setup._AncsAdvert("hci7", ADVERT_PATH)
    return None, (ADVERT_PATH, "org.freedesktop.DBus.Properties", "Get")


def _activation(connection, effects, monkeypatch):
    monkeypatch.setattr(glib_client_activation, "get_session_bus", lambda: connection)
    monkeypatch.setattr(glib_client_activation, "record_client_use", lambda _key: None)
    activation = glib_client_activation.ClientActivation(
        "gtk", lambda handle, token: effects.append((handle, token)),
    )
    return activation.close, (ACTIVATION_PATH, ACTIVATION_INTERFACE, "OpenMessage")


def _mpris(connection, effects, _monkeypatch):
    media = SimpleNamespace(
        available=True,
        state=SimpleNamespace(track_serial=1, playback_state=None, title="Title"),
        add_listener=lambda _listener: None,
        remove_listener=lambda _listener: None,
    )
    guard = SimpleNamespace(authorize=lambda _sender, action: effects.append(action))
    # The player exports itself before it asks for its name; that request is
    # left unanswered so the object stays reachable at the unique name.
    claim = SimpleNamespace(want=lambda _wanted, _listener=None: None)
    player = MprisPlayer(connection, media, guard, claim=claim)
    return player.close, (MPRIS_PATH, PLAYER_IFACE, "Seek")


# (export, correctly typed call, its reply, its effects, wrongly typed call).
# Each wrong call is one the method would otherwise accept and act on.
CASES = {
    "messages": (
        _messages, ((False,), "b"), [], [False], (("false",), "s"),
    ),
    "pairing-agent": (
        _agent,
        ((dbus.ObjectPath(DEVICE), 123456, 0), "ouq"), [], [123456],
        ((DEVICE, 123456, 0), "suq"),
    ),
    "ancs-advert": (
        _advert,
        (("org.bluez.LEAdvertisement1", "Type"), "ss"), ["peripheral"], [],
        (("org.bluez.LEAdvertisement1", "Type"), "sv"),
    ),
    "client-activation": (
        _activation,
        (("handle", "token"), "ss"), [], [("handle", "token")],
        (("handle", dbus.ObjectPath("/token")), "so"),
    ),
    "mpris": (
        _mpris, ((5,), "x"), [], ["media-command"], (("5",), "s"),
    ),
}


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


@pytest.fixture
def exported(monkeypatch):
    connection = open_private_bus()
    cleanups = []

    def export(case):
        effects: list = []
        close, target = CASES[case][0](connection, effects, monkeypatch)
        if close is not None:
            cleanups.append(close)
        return target, effects

    try:
        yield connection.get_unique_name(), export
    finally:
        # A BusName releases its name again when collected, which fails
        # once the connection is closed, so drop each object first.
        while cleanups:
            cleanups.pop()()
        connection.close()


def _raw_call(destination, target, call):
    (path, interface, member), (values, signature) = target, call
    outcome = {}

    def run() -> None:
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            message = dbus.lowlevel.MethodCallMessage(destination, path, interface, member)
            message.append(*values, signature=signature)
            reply = connection.send_message_with_reply_and_block(message, 5)
            outcome["reply"] = reply.get_args_list()
        except dbus.exceptions.DBusException as error:
            outcome["error"] = error.get_dbus_name()
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join(timeout=1)
    return outcome


@pytest.mark.parametrize("case", CASES)
def test_call_with_the_wrong_argument_types_is_rejected(exported, case) -> None:
    destination, export = exported
    target, effects = export(case)

    outcome = _raw_call(destination, target, CASES[case][4])

    assert outcome == {"error": "org.freedesktop.DBus.Error.InvalidArgs"}
    assert effects == []


@pytest.mark.parametrize("case", CASES)
def test_call_with_the_declared_argument_types_still_works(exported, case) -> None:
    destination, export = exported
    target, effects = export(case)
    _export, call, reply, expected, _wrong = CASES[case]

    outcome = _raw_call(destination, target, call)

    assert outcome == {"reply": reply}
    assert effects == expected
