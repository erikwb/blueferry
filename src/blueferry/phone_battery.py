"""Read the iPhone's battery over Bluetooth LE, without the hands-free profile.

iOS exposes the standard GATT Battery Service (0x180F) with its Battery Level
characteristic (0x2A19, one byte, 0-100 %) to the LE peer it is connected to,
which BlueFerry already is for ANCS. Where BlueZ's battery plugin claims that
service it also publishes ``org.bluez.Battery1.Percentage`` on the device;
that value is preferred, the characteristic is the fallback.

Everything here is asynchronous: GetManagedObjects, ReadValue, StartNotify and
StopNotify are sent with reply handlers, and updates arrive as
PropertiesChanged signals. Values are range-checked; anything malformed is
"unknown".
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import dbus

from blueferry.bus import get_system_bus

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
BATTERY1_IFACE = "org.bluez.Battery1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
CALL_TIMEOUT_SEC = 25

SOURCE_BLUEZ = "bluez"
SOURCE_GATT = "gatt"


def _percent(value: object) -> int | None:
    # dbus.Byte is an int subclass; dbus.Boolean (and bool) must not count.
    if isinstance(value, bool) or type(value).__name__ == "Boolean":
        return None
    if not isinstance(value, int):
        return None
    selected = int(value)
    return selected if 0 <= selected <= 100 else None


def parse_battery_level(value: object) -> int | None:
    """Decode a Battery Level characteristic value (``ay``) to 0-100."""
    if isinstance(value, bytes | bytearray):
        data = list(value)
    elif isinstance(value, list | tuple):
        data = list(value)
    else:
        return None
    if len(data) != 1:
        return None
    return _percent(data[0])


def parse_battery1_percentage(value: object) -> int | None:
    return _percent(value)


def _under(path: str, device_path: str) -> bool:
    return path.startswith(device_path + "/")


class PhoneBattery:
    """Track one device's battery level from Battery1 or the GATT characteristic."""

    def __init__(
        self,
        device_path: str,
        *,
        on_change: Callable[[], None] | None = None,
        bus_factory: Callable[[], Any] = get_system_bus,
    ) -> None:
        self.device_path = device_path
        self._on_change = on_change or (lambda: None)
        self._bus_factory = bus_factory
        self._running = False
        # start() was requested and stop() has not been: an owner change
        # brings the watcher back with the new bluetoothd.
        self._wanted = False
        self._generation = 0
        self._matches: list[Any] = []
        self._char_match: Any = None
        self._char_path: str | None = None
        self._notifying = False
        self._battery1: int | None = None
        self._gatt: int | None = None
        self._logged: set[str] = set()

    # ---- public state ---------------------------------------------------

    @property
    def percent(self) -> int | None:
        return self._battery1 if self._battery1 is not None else self._gatt

    @property
    def source(self) -> str | None:
        if self._battery1 is not None:
            return SOURCE_BLUEZ
        if self._gatt is not None:
            return SOURCE_GATT
        return None

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._wanted = True
        if self._running:
            return
        self._running = True
        self._generation += 1
        try:
            bus = self._bus_factory()
            for signal, handler in (
                ("InterfacesAdded", self._on_added),
                ("InterfacesRemoved", self._on_removed),
            ):
                self._matches.append(bus.add_signal_receiver(
                    self._guard(handler), signal_name=signal,
                    dbus_interface=OBJECT_MANAGER_IFACE, bus_name=BLUEZ, path="/",
                ))
            self._matches.append(bus.add_signal_receiver(
                self._guard(self._on_battery1_changed), signal_name="PropertiesChanged",
                dbus_interface=PROPERTIES_IFACE, bus_name=BLUEZ,
                path=self.device_path, arg0=BATTERY1_IFACE,
            ))
        except Exception:
            log.info("could not watch the iPhone's battery", exc_info=True)
            self._halt()
            return
        self._call(
            "/", OBJECT_MANAGER_IFACE, "GetManagedObjects", "", (),
            self._on_managed, self._failed("GetManagedObjects"),
        )

    def stop(self) -> None:
        self._wanted = False
        self._halt()

    def _halt(self) -> None:
        self._running = False
        self._generation += 1
        self._forget_characteristic(stop_notify=True)
        for match in self._matches:
            self._remove(match)
        self._matches = []
        changed = self._battery1 is not None
        self._battery1 = None
        if changed:
            self._changed()

    def bluez_owner_changed(self, present: bool) -> None:
        """bluetoothd left or restarted: every object and session is new."""
        self._notifying = False  # the old bluetoothd took the session along
        wanted = self._wanted
        self._halt()
        if present and wanted:
            self.start()

    def link_up(self) -> None:
        """The LE link (re)connected: notification sessions start over."""
        if not self._running or self._char_path is None:
            return
        self._notifying = False
        self._subscribe(self._char_path)

    # ---- internals ------------------------------------------------------

    def _guard(self, handler: Callable[..., None]) -> Callable[..., None]:
        generation = self._generation

        def guarded(*args: Any, **kwargs: Any) -> None:
            if not self._running or generation != self._generation:
                return
            try:
                handler(*args, **kwargs)
            except Exception:
                log.exception("phone battery signal handling failed")

        return guarded

    def _call(
        self,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: tuple[Any, ...],
        on_reply: Callable[..., None],
        on_error: Callable[[Exception], None],
    ) -> None:
        generation = self._generation

        def replied(*values: Any) -> None:
            if self._running and generation == self._generation:
                on_reply(*values)

        def failed(error: Exception) -> None:
            if self._running and generation == self._generation:
                on_error(error)

        try:
            self._bus_factory().call_async(
                BLUEZ, path, interface, method, signature, args,
                replied, failed, timeout=CALL_TIMEOUT_SEC,
            )
        except Exception as error:
            failed(error)

    def _failed(self, what: str) -> Callable[[Exception], None]:
        def failed(error: Exception) -> None:
            name = (
                error.get_dbus_name() if isinstance(error, dbus.exceptions.DBusException)
                else type(error).__name__
            )
            # The iPhone may be out of range or between links; say it once.
            if what not in self._logged:
                self._logged.add(what)
                log.info("iPhone battery %s failed: %s", what, name)

        return failed

    def _changed(self) -> None:
        try:
            self._on_change()
        except Exception:
            log.exception("phone battery callback failed")

    @staticmethod
    def _remove(match: Any) -> None:
        if match is None:
            return
        try:
            match.remove()
        except Exception:
            log.debug("could not remove phone battery watch", exc_info=True)

    def _on_managed(self, objects: object = None) -> None:
        if not isinstance(objects, Mapping):
            return
        for path, interfaces in objects.items():
            self._on_added(path, interfaces)

    def _on_added(self, path: object, interfaces: object) -> None:
        path = str(path)
        if not isinstance(interfaces, Mapping):
            return
        battery = interfaces.get(BATTERY1_IFACE)
        if path == self.device_path and isinstance(battery, Mapping):
            self._set_battery1(parse_battery1_percentage(battery.get("Percentage")))
        char = interfaces.get(GATT_CHAR_IFACE)
        if (
            isinstance(char, Mapping) and _under(path, self.device_path)
            and str(char.get("UUID", "")).lower() == BATTERY_LEVEL_UUID
            and self._char_path != path
        ):
            self._adopt_characteristic(path, char)

    def _on_removed(self, path: object, interfaces: object) -> None:
        path = str(path)
        names = (
            {str(name) for name in interfaces} if isinstance(interfaces, list | tuple) else set()
        )
        if path == self.device_path and BATTERY1_IFACE in names:
            self._set_battery1(None)
        if path == self._char_path and GATT_CHAR_IFACE in names:
            self._forget_characteristic(stop_notify=False)

    def _on_battery1_changed(self, interface: object, changed: object, _invalidated=None) -> None:
        if str(interface) == BATTERY1_IFACE and isinstance(changed, Mapping):
            if "Percentage" in changed:
                self._set_battery1(parse_battery1_percentage(changed["Percentage"]))

    def _set_battery1(self, value: int | None) -> None:
        if value == self._battery1:
            return
        previous = self.percent
        self._battery1 = value
        if self.percent != previous or value is None:
            self._changed()

    def _set_gatt(self, value: int | None) -> None:
        if value == self._gatt:
            return
        previous = self.percent
        self._gatt = value
        if self.percent != previous:
            self._changed()

    def _adopt_characteristic(self, path: str, properties: Mapping[Any, Any]) -> None:
        self._forget_characteristic(stop_notify=True)
        self._char_path = path
        try:
            self._char_match = self._bus_factory().add_signal_receiver(
                self._guard(self._on_char_changed), signal_name="PropertiesChanged",
                dbus_interface=PROPERTIES_IFACE, bus_name=BLUEZ,
                path=path, arg0=GATT_CHAR_IFACE,
            )
        except Exception:
            log.info("could not watch the iPhone's battery level", exc_info=True)
            self._char_path = None
            return
        log.info("found the iPhone's GATT battery level")
        cached = parse_battery_level(properties.get("Value"))
        if cached is not None:
            self._set_gatt(cached)
        # "Notifying" may already be true for another client's session; ours
        # still needs its own StartNotify.
        self._notifying = False
        self._subscribe(path)

    def _subscribe(self, path: str) -> None:
        """Read once, then follow notifications (one session per D-Bus client)."""
        self._call(
            path, GATT_CHAR_IFACE, "ReadValue", "a{sv}",
            (dbus.Dictionary({}, signature="sv"),),
            lambda value=None: self._set_gatt(parse_battery_level(value)),
            self._failed("ReadValue"),
        )
        if self._notifying:
            return

        def started(*_values: Any) -> None:
            self._notifying = True

        self._call(
            path, GATT_CHAR_IFACE, "StartNotify", "", (), started, self._failed("StartNotify"),
        )

    def _on_char_changed(self, interface: object, changed: object, _invalidated=None) -> None:
        if str(interface) == GATT_CHAR_IFACE and isinstance(changed, Mapping):
            if "Value" in changed:
                self._set_gatt(parse_battery_level(changed["Value"]))

    def _forget_characteristic(self, *, stop_notify: bool) -> None:
        path, self._char_path = self._char_path, None
        self._remove(self._char_match)
        self._char_match = None
        if path is not None and stop_notify and self._notifying:
            try:
                self._bus_factory().call_async(
                    BLUEZ, path, GATT_CHAR_IFACE, "StopNotify", "", (),
                    lambda *_values: None,
                    lambda _error: None,
                    timeout=CALL_TIMEOUT_SEC,
                )
            except Exception:
                log.debug("could not stop battery notifications", exc_info=True)
        self._notifying = False
        self._set_gatt(None)


class BatteryWarningSettings:
    """The saved low-battery warning opt-in (``settings.json``).

    ``BLUEFERRY_PHONE_BATTERY_NOTIFY`` seeds the initial value; a value saved
    through the D-Bus API (Qt settings, ``blueferry phone-status warn``)
    wins. The threshold stays ``BLUEFERRY_PHONE_BATTERY_LOW_PERCENT``.
    """

    ENABLED_KEY = "phone_battery_warning"

    def __init__(self, path: Any = None, *, default_enabled: bool | None = None) -> None:
        from blueferry import config
        from blueferry.settings_store import SettingsStore

        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read().get(self.ENABLED_KEY)
        seeded = config.PHONE_BATTERY_NOTIFY if default_enabled is None else default_enabled
        self._enabled = stored if isinstance(stored, bool) else bool(seeded)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("battery warning enabled must be a boolean")
        self._settings.update(**{self.ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled
