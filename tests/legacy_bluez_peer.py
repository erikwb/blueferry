"""A stand-in bluetoothd without bearer interfaces, run as a test subprocess.

It models the parts of BlueZ before 5.84 that ANCS depends on, following
src/device.c and src/gatt-client.c:

- no org.bluez.Bearer interfaces; Device1.Connected is true for either bearer;
- Device1.ServicesResolved clears when a bearer drops;
- StartNotify without an ATT link succeeds and completes on connection, and a
  repeat meanwhile is InProgress;
- WriteValue without an ATT link fails locally as "Not connected".
"""
from __future__ import annotations

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
SERVICE = f"{DEVICE}/service0010"
NOTIFICATION_SOURCE = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"
CHARACTERISTICS = {
    f"{SERVICE}/char0011": NOTIFICATION_SOURCE,
    f"{SERVICE}/char0014": CONTROL_POINT,
    f"{SERVICE}/char0016": DATA_SOURCE,
}
PROPERTIES = "org.freedesktop.DBus.Properties"
CHARACTERISTIC = "org.bluez.GattCharacteristic1"
CONTROL = "io.weirdware.BlueFerry.TestPeer"


def _error(name: str, message: str) -> dbus.exceptions.DBusException:
    return dbus.exceptions.DBusException(message, name=name)


class Peer(dbus.service.Object):
    def __init__(self, bus) -> None:
        super().__init__(bus, "/")
        self.classic = self.le = self.resolved = False
        self.answer = True
        self.starts = self.stops = self.writes = 0
        self.characteristics = {
            path: Characteristic(bus, path, uuid, self)
            for path, uuid in CHARACTERISTICS.items()
        }
        self.device = Device(bus, self)

    @dbus.service.method(
        "org.freedesktop.DBus.ObjectManager", out_signature="a{oa{sa{sv}}}",
    )
    def GetManagedObjects(self):
        managed = {DEVICE: {"org.bluez.Device1": {"Connected": self.connected}}}
        for path, characteristic in self.characteristics.items():
            managed[path] = {CHARACTERISTIC: {"UUID": characteristic.uuid}}
        return managed

    @property
    def connected(self) -> bool:
        return self.classic or self.le

    @dbus.service.method(CONTROL, in_signature="b")
    def SetClassic(self, connected):
        self.classic = bool(connected)
        if not connected:
            self.resolved = False

    @dbus.service.method(CONTROL, in_signature="b")
    def SetLe(self, connected):
        self.le = bool(connected)
        self.resolved = self.le
        for characteristic in self.characteristics.values():
            characteristic.le_changed()

    @dbus.service.method(CONTROL, in_signature="b")
    def SetAnswering(self, answering):
        self.answer = bool(answering)

    @dbus.service.method(CONTROL, out_signature="uuu")
    def Counters(self):
        return self.starts, self.stops, self.writes

    def respond(self, packet: bytes) -> None:
        """Answer GetAppAttributes on Data Source, as iOS does when authorized."""
        data_source = next(
            value for value in self.characteristics.values()
            if value.uuid == DATA_SOURCE
        )
        if not self.answer or not data_source.enabled or packet[:1] != b"\x01":
            return
        app_id = packet[1:packet.index(b"\0", 1)]
        name = b"Messages"
        response = (
            b"\x01" + app_id + b"\0" + b"\x00"
            + len(name).to_bytes(2, "little") + name
        )
        data_source.PropertiesChanged(
            CHARACTERISTIC, {"Value": dbus.ByteArray(response)}, [],
        )


class Device(dbus.service.Object):
    def __init__(self, bus, peer: Peer) -> None:
        super().__init__(bus, DEVICE)
        self.peer = peer

    @dbus.service.method(PROPERTIES, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        if interface != "org.bluez.Device1":
            raise _error(
                "org.freedesktop.DBus.Error.InvalidArgs",
                f"No such interface '{interface}'",
            )
        if name == "Connected":
            return dbus.Boolean(self.peer.connected)
        if name == "ServicesResolved":
            return dbus.Boolean(self.peer.resolved)
        raise _error(
            "org.freedesktop.DBus.Error.InvalidArgs", f"No such property '{name}'",
        )


class Characteristic(dbus.service.Object):
    def __init__(self, bus, path: str, uuid: str, peer: Peer) -> None:
        super().__init__(bus, path)
        self.uuid, self.peer = uuid, peer
        self.registered = self.enabled = self.notifying = False

    def le_changed(self) -> None:
        # Registrations survive a disconnect and are completed on reconnect;
        # the Notifying flag is not cleared in between.
        self.enabled = self.registered and self.peer.le
        self.notifying = self.notifying or self.enabled

    @dbus.service.method(PROPERTIES, in_signature="ss", out_signature="v")
    def Get(self, _interface, name):
        if name != "Notifying":
            raise _error(
                "org.freedesktop.DBus.Error.InvalidArgs",
                f"No such property '{name}'",
            )
        return dbus.Boolean(self.notifying)

    @dbus.service.signal(PROPERTIES, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    @dbus.service.method(CHARACTERISTIC)
    def StartNotify(self):
        self.peer.starts += 1
        if self.registered and not self.enabled:
            raise _error("org.bluez.Error.InProgress", "Operation already in progress")
        self.registered = True
        self.le_changed()

    @dbus.service.method(CHARACTERISTIC)
    def StopNotify(self):
        self.peer.stops += 1
        if not self.registered:
            raise _error("org.bluez.Error.Failed", "No notify session started")
        self.registered = self.enabled = self.notifying = False

    @dbus.service.method(CHARACTERISTIC, in_signature="aya{sv}")
    def WriteValue(self, value, _options):
        if not self.peer.le:
            raise _error("org.bluez.Error.Failed", "Not connected")
        self.peer.writes += 1
        self.peer.respond(bytes(value))


def main() -> None:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus(private=True)
    peer = Peer(bus)
    owner = bus.request_name("org.bluez", dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    # A leftover owner would silently stand in for this peer.
    assert owner == dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER and peer is not None
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
