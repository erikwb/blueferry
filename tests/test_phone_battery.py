"""LE battery of the phone (Battery1 / GATT Battery Service) against a fake bus."""
from __future__ import annotations

import dbus
import pytest
from hypothesis import given
from hypothesis import strategies as st

from blueferry.phone_battery import (
    BATTERY1_IFACE,
    BATTERY_LEVEL_UUID,
    GATT_CHAR_IFACE,
    PhoneBattery,
    parse_battery1_percentage,
    parse_battery_level,
)

DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
CHAR = f"{DEVICE}/service0019/char001a"


class Match:
    def __init__(self, handler, kwargs) -> None:
        self.handler = handler
        self.kwargs = kwargs
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class FakeBus:
    def __init__(self) -> None:
        self.matches: list[Match] = []
        self.calls: list[tuple] = []

    def add_signal_receiver(self, handler, **kwargs):
        match = Match(handler, kwargs)
        self.matches.append(match)
        return match

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply_handler, error_handler, timeout=None):
        assert bus_name == "org.bluez" and timeout
        self.calls.append((path, interface, method, signature, args, reply_handler, error_handler))

    def take(self, method):
        for index, call in enumerate(self.calls):
            if call[2] == method:
                return self.calls.pop(index)
        raise AssertionError(f"no {method}: {[c[2] for c in self.calls]}")

    def emit(self, signal, *args, path=None, arg0=None):
        for match in list(self.matches):
            kwargs = match.kwargs
            if match.removed or kwargs["signal_name"] != signal:
                continue
            if path is not None and kwargs.get("path") not in (None, path):
                continue
            if arg0 is not None and kwargs.get("arg0") not in (None, arg0):
                continue
            match.handler(*args)


def _char(value=None):
    props = {"UUID": dbus.String(BATTERY_LEVEL_UUID), "Notifying": dbus.Boolean(True)}
    if value is not None:
        props["Value"] = dbus.Array([dbus.Byte(value)], signature="y")
    return {GATT_CHAR_IFACE: props}


def _battery(**kwargs):
    bus = FakeBus()
    changes = []
    watcher = PhoneBattery(DEVICE, on_change=lambda: changes.append(True),
                           bus_factory=lambda: bus, **kwargs)
    return watcher, bus, changes


@pytest.mark.parametrize("value,expected", [
    (dbus.Array([dbus.Byte(91)], signature="y"), 91), (b"\x00", 0), ([100], 100),
    ([101], None), ([], None), ([1, 2], None), ("91", None), (None, None),
    ([dbus.Boolean(True)], None),
])
def test_battery_level_value_is_one_byte_percent(value, expected) -> None:
    assert parse_battery_level(value) == expected


@given(st.one_of(st.none(), st.integers(), st.binary(), st.lists(st.integers()), st.text()))
def test_battery_parsers_never_crash_and_stay_in_range(value) -> None:
    for parsed in (parse_battery_level(value), parse_battery1_percentage(value)):
        assert parsed is None or 0 <= parsed <= 100


def test_gatt_battery_is_read_and_followed_asynchronously() -> None:
    watcher, bus, changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({
        dbus.ObjectPath(CHAR): _char(91),
        dbus.ObjectPath("/org/bluez/hci0/dev_11_22_33_44_55_66/service0019/char001a"): _char(5),
    })

    assert (watcher.percent, watcher.source) == (91, "gatt")
    read = bus.take("ReadValue")
    assert read[0] == CHAR and read[3] == "a{sv}" and dict(read[4][0]) == {}
    read[5](dbus.Array([dbus.Byte(90)], signature="y"))
    bus.take("StartNotify")[5]()
    bus.emit("PropertiesChanged", GATT_CHAR_IFACE,
             {"Value": dbus.Array([dbus.Byte(89)], signature="y")}, [], path=CHAR,
             arg0=GATT_CHAR_IFACE)

    assert watcher.percent == 89
    assert len(changes) == 3

    watcher.stop()
    assert bus.take("StopNotify")[0] == CHAR
    assert watcher.percent is None
    assert all(match.removed for match in bus.matches)


def test_battery1_is_followed_while_the_characteristic_has_no_value() -> None:
    watcher, bus, _changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({
        dbus.ObjectPath(DEVICE): {BATTERY1_IFACE: {"Percentage": dbus.Byte(70)}},
    })
    assert (watcher.percent, watcher.source) == (70, "bluez")

    bus.emit("PropertiesChanged", BATTERY1_IFACE, {"Percentage": dbus.Byte(69)}, [],
             path=DEVICE, arg0=BATTERY1_IFACE)
    assert watcher.percent == 69
    bus.emit("InterfacesRemoved", dbus.ObjectPath(DEVICE), [BATTERY1_IFACE])
    assert (watcher.percent, watcher.source) == (None, None)


def test_the_phones_own_level_wins_over_a_stale_battery1() -> None:
    # bluetoothd 5.87 after an LE reconnect: Battery1 keeps the old level
    # ("error registering battery: path exists") while the phone reports 97.
    watcher, bus, changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({
        dbus.ObjectPath(DEVICE): {BATTERY1_IFACE: {"Percentage": dbus.Byte(100)}},
        dbus.ObjectPath(CHAR): _char(97),
    })
    assert (watcher.percent, watcher.source) == (97, "gatt")

    bus.emit("PropertiesChanged", GATT_CHAR_IFACE, {"Value": dbus.Array([dbus.Byte(96)])}, [],
             path=CHAR, arg0=GATT_CHAR_IFACE)
    assert (watcher.percent, watcher.source) == (96, "gatt")
    seen = len(changes)
    bus.emit("PropertiesChanged", BATTERY1_IFACE, {"Percentage": dbus.Byte(99)}, [],
             path=DEVICE, arg0=BATTERY1_IFACE)
    assert (watcher.percent, watcher.source) == (96, "gatt")
    assert len(changes) == seen

    bus.emit("InterfacesRemoved", dbus.ObjectPath(CHAR), [GATT_CHAR_IFACE])
    assert (watcher.percent, watcher.source) == (99, "bluez")


def test_characteristic_appearing_later_is_adopted_and_removal_forgets_it() -> None:
    watcher, bus, _changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({})
    assert watcher.percent is None

    bus.emit("InterfacesAdded", dbus.ObjectPath(CHAR), _char(40))
    assert watcher.percent == 40
    bus.emit("InterfacesRemoved", dbus.ObjectPath(CHAR), [GATT_CHAR_IFACE])
    assert watcher.percent is None


def test_failures_are_quiet_and_never_raise(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="blueferry.phone_battery")
    watcher, bus, _changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({dbus.ObjectPath(CHAR): _char()})
    error = dbus.exceptions.DBusException("x", name="org.bluez.Error.NotConnected")
    bus.take("ReadValue")[6](error)
    bus.take("StartNotify")[6](error)
    watcher.link_up()
    bus.take("ReadValue")[6](error)

    assert watcher.percent is None
    assert sum("ReadValue failed" in record.message for record in caplog.records) == 1


def test_owner_change_drops_stale_replies_and_restarts_only_when_wanted() -> None:
    watcher, bus, _changes = _battery()
    watcher.start()
    stale = bus.take("GetManagedObjects")
    watcher.bluez_owner_changed(True)
    stale[5]({dbus.ObjectPath(CHAR): _char(50)})
    assert watcher.percent is None
    bus.take("GetManagedObjects")[5]({dbus.ObjectPath(CHAR): _char(50)})
    assert watcher.percent == 50

    watcher.stop()
    bus.calls.clear()
    watcher.bluez_owner_changed(True)
    assert bus.calls == []


def test_a_missing_bus_keeps_the_daemon_running() -> None:
    def broken():
        raise dbus.exceptions.DBusException("no bus", name="org.freedesktop.DBus.Error.NoServer")

    watcher = PhoneBattery(DEVICE, bus_factory=broken)
    watcher.start()
    assert watcher.percent is None
    watcher.stop()


def test_removed_characteristic_drops_pending_read_and_notify_replies() -> None:
    watcher, bus, _changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({dbus.ObjectPath(CHAR): _char(50)})
    read, notify = bus.take("ReadValue"), bus.take("StartNotify")
    bus.emit("InterfacesRemoved", dbus.ObjectPath(CHAR), [GATT_CHAR_IFACE])

    read[5](dbus.Array([dbus.Byte(49)], signature="y"))
    notify[5]()
    assert watcher.percent is None
    watcher.stop()
    assert not any(call[2] == "StopNotify" for call in bus.calls)


def test_recreated_characteristic_at_same_path_ignores_old_values() -> None:
    watcher, bus, _changes = _battery()
    watcher.start()
    bus.take("GetManagedObjects")[5]({dbus.ObjectPath(CHAR): _char(50)})
    read = bus.take("ReadValue")
    old_watch = next(match for match in bus.matches if match.kwargs.get("path") == CHAR)
    bus.emit("InterfacesRemoved", dbus.ObjectPath(CHAR), [GATT_CHAR_IFACE])
    bus.emit("InterfacesAdded", dbus.ObjectPath(CHAR), _char(80))

    read[5](dbus.Array([dbus.Byte(49)], signature="y"))
    old_watch.handler(GATT_CHAR_IFACE, {"Value": [48]}, [])
    assert watcher.percent == 80
    bus.take("ReadValue")[5](dbus.Array([dbus.Byte(79)], signature="y"))
    assert watcher.percent == 79
