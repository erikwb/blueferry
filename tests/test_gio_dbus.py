"""GioDBus against a real GDBus service on the private test bus."""
from __future__ import annotations

import os
import time

import pytest
from gi.repository import Gio, GLib

from blueferry.gio_dbus import NO_REPLY, DBusCallError, GioDBus, error_name

pytestmark = pytest.mark.private_dbus

_XML = """
<node>
  <interface name="org.example.Pilot">
    <method name="Echo">
      <arg type="s" direction="in"/><arg type="a{sv}" direction="in"/>
      <arg type="s" direction="out"/><arg type="u" direction="out"/>
    </method>
    <method name="Fail"/>
    <method name="Hang"/>
    <signal name="Changed"><arg type="s"/><arg type="as"/></signal>
  </interface>
</node>
"""


def _connect() -> Gio.DBusConnection:
    return Gio.DBusConnection.new_for_address_sync(
        os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None,
    )


def _wait(condition) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "no reply from the private bus"
        context.iteration(False) or time.sleep(0.005)


@pytest.fixture
def service():
    connection = _connect()
    held: list = []

    def method_call(_conn, _sender, _path, _iface, method, params, invocation):
        if method == "Echo":
            text, options = params.unpack()
            invocation.return_value(GLib.Variant("(su)", (text, len(options))))
        elif method == "Fail":
            invocation.return_dbus_error("org.bluez.Error.Failed", "nope")
        else:
            held.append(invocation)  # never answered: the caller times out

    info = Gio.DBusNodeInfo.new_for_xml(_XML).interfaces[0]
    # GLib 2.84 deprecated register_object; older distributions lack its successor.
    register = getattr(connection, "register_object_with_closures2", None)
    registration = (register or connection.register_object)(
        "/pilot", info, method_call, None, None,
    )
    yield connection
    connection.unregister_object(registration)
    connection.close_sync(None)


def test_call_types_arguments_and_unpacks_the_reply(service) -> None:
    client = _connect()
    bus = GioDBus(lambda: client)
    replies: list = []
    bus.call(
        service.get_unique_name(), "/pilot", "org.example.Pilot", "Echo", "sa{sv}",
        ("hello", {}), "(su)", lambda *reply: replies.append(reply), pytest.fail,
        timeout=5,
    )
    _wait(lambda: replies)
    # An empty a{sv} marshals because the type comes from the signature.
    assert replies == [("hello", 0)]
    with pytest.raises(TypeError):
        bus.call(
            service.get_unique_name(), "/pilot", "org.example.Pilot", "Echo", "sa{sv}",
            (1, {}), "(su)", pytest.fail, pytest.fail, timeout=5,
        )
    client.close_sync(None)


def test_remote_errors_and_timeouts_keep_dbus_names(service) -> None:
    client = _connect()
    bus = GioDBus(lambda: client)
    errors: list = []
    for method, timeout in (("Fail", 5), ("Hang", 0.05)):
        bus.call(
            service.get_unique_name(), "/pilot", "org.example.Pilot", method, "", (),
            "()", pytest.fail, errors.append, timeout=timeout,
        )
    _wait(lambda: len(errors) == 2)
    assert all(isinstance(error, DBusCallError) for error in errors)
    assert [error_name(error) for error in errors] == ["org.bluez.Error.Failed", NO_REPLY]
    assert str(errors[0]) == "org.bluez.Error.Failed: nope"
    client.close_sync(None)


def test_subscription_delivers_unpacked_arguments_until_removed(service) -> None:
    client = _connect()
    bus = GioDBus(lambda: client)
    received: list = []
    subscription = bus.subscribe(
        service.get_unique_name(), "org.example.Pilot", "Changed",
        lambda *args: received.append(args), path="/pilot", arg0="device",
    )

    def emit(first: str) -> None:
        service.emit_signal(
            None, "/pilot", "org.example.Pilot", "Changed",
            GLib.Variant("(sas)", (first, ["a"])),
        )

    emit("other")  # filtered by arg0
    emit("device")
    _wait(lambda: received)
    assert received == [("device", ["a"])]
    subscription.remove()
    subscription.remove()  # idempotent
    emit("device")
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
    assert received == [("device", ["a"])]
    located: list = []
    subscription = bus.subscribe(
        service.get_unique_name(), "org.example.Pilot", "Changed",
        lambda *args: located.append(args), arg0="device", with_path=True,
    )
    emit("device")
    _wait(lambda: located)
    assert located == [("/pilot", "device", ["a"])]
    subscription.remove()
    client.close_sync(None)


def test_audio_route_follows_a_replacement_bluez_owner():
    from blueferry.phone_audio_route import A2DP_SINK_UUID, A2DP_SOURCE_UUID, PhoneAudioRoute

    device = "/org/bluez/hci0/dev_AA"
    objects = {
        device: {"org.bluez.Device1": {
            "Connected": GLib.Variant("b", True),
            "UUIDs": GLib.Variant("as", [A2DP_SOURCE_UUID]),
        }},
        device + "/fd0": {"org.bluez.MediaTransport1": {
            "Device": GLib.Variant("o", device),
            "UUID": GLib.Variant("s", A2DP_SINK_UUID),
            "State": GLib.Variant("s", "active"),
        }},
    }
    info = Gio.DBusNodeInfo.new_for_xml('''
        <node><interface name="org.freedesktop.DBus.ObjectManager">
        <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/>
        </method></interface></node>
    ''').interfaces[0]

    def serve(connection):
        def reply(_conn, _sender, _path, _iface, _method, _params, invocation):
            invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))

        register = getattr(connection, "register_object_with_closures2", None)
        registration = (register or connection.register_object)("/", info, reply, None, None)
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", GLib.Variant("(su)", ("org.bluez", 0)),
            GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 3000, None,
        )
        assert result.unpack() == (1,)
        return registration

    old, new, client = _connect(), _connect(), _connect()
    registrations = []
    bus = GioDBus(lambda: client)
    subject = PhoneAudioRoute(
        bus, device, allowed=True, on_changed=lambda: None,
        schedule=lambda *_: 1, cancel=lambda _: None,
    )
    watch = bus.subscribe(
        "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
        lambda _name, _old, owner: subject.bluez_owner_changed(bool(owner)), arg0="org.bluez",
    )
    try:
        registrations.append((old, serve(old)))
        subject.start()
        _wait(lambda: subject.snapshot()["phone_audio_playing"] == "pc")
        old.close_sync(None)
        _wait(lambda: subject.snapshot()["phone_audio_playing"] == "unknown")
        assert subject.snapshot()["phone_audio_route"] == "unavailable"
        device_interfaces = objects[device]
        objects.clear()
        registrations.append((new, serve(new)))
        _wait(lambda: subject.snapshot()["phone_audio_reason"] == "phone_disconnected")
        # BlueZ may export the phone after its early GetManagedObjects reply.
        objects[device] = device_interfaces
        new.emit_signal(
            None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
            GLib.Variant("(oa{sa{sv}})", (device, device_interfaces)),
        )
        _wait(lambda: subject.snapshot()["phone_audio_route"] == "phone")
        assert subject.snapshot()["phone_audio_playing"] == "unknown"
    finally:
        subject.stop()
        watch.remove()
        for connection, registration in registrations:
            connection.unregister_object(registration)
        for connection in (old, new, client):
            if not connection.is_closed():
                connection.close_sync(None)
