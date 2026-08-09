"""Keep the iPhone's classic and LE Bluetooth bearers connected."""

from __future__ import annotations

import logging
from collections.abc import Callable

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.bus import get_system_bus

log = logging.getLogger(__name__)

POLL_SECONDS = 5
CLASSIC_SETTLE_SECONDS = 3
_INTERFACES = {
    "bredr": "org.bluez.Bearer.BREDR1",
    "le": "org.bluez.Bearer.LE1",
}

ReadConnected = Callable[[str], bool | None]
Connect = Callable[[str, Callable[[], None], Callable[[Exception], None]], None]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


class BearerSupervisor:
    """Connect BR/EDR first, then keep LE connected alongside it.

    Pairing creates bonds, not a durable connection policy. Desktop Bluetooth
    applets often connect a newly paired device as a side effect; this class
    makes the backend independent of that desktop-specific behavior.
    """

    def __init__(
        self,
        device_path: str,
        *,
        on_status: Callable[[], None] | None = None,
        read_connected: ReadConnected | None = None,
        connect: Connect | None = None,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
    ) -> None:
        self.device_path = device_path
        self._on_status = on_status
        self._read_connected = read_connected or self._read_bluez_connected
        self._connect = connect or self._connect_bluez
        self._schedule = schedule
        self._cancel = cancel
        self._timer_id: int | None = None
        self._le_settle_id: int | None = None
        self._running = False
        self._connecting: set[str] = set()
        self._last_errors: dict[str, str] = {}
        self._states: dict[str, bool | None] = {"bredr": None, "le": None}

    @property
    def bredr_connected(self) -> bool:
        return self._states["bredr"] is True

    @property
    def le_connected(self) -> bool:
        return self._states["le"] is True

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()
        self._timer_id = self._schedule(POLL_SECONDS, self._tick)

    def poke(self) -> None:
        """Run a health check now, for example after system resume."""
        if self._running:
            self._tick()

    def stop(self) -> None:
        self._running = False
        self._connecting.clear()
        self._cancel_le_settle()
        if self._timer_id is not None:
            try:
                self._cancel(self._timer_id)
            except Exception:
                log.debug("could not remove bearer health timer", exc_info=True)
            self._timer_id = None

    def snapshot(self) -> dict[str, bool]:
        return {
            "bredr": self.bredr_connected,
            "le": self.le_connected,
        }

    def _tick(self) -> bool:
        if not self._running:
            return False

        bredr = self._read("bredr")
        le = self._read("le")
        self._update_state("bredr", bredr)
        self._update_state("le", le)

        # Establish the normal Bluetooth ACL/profile connection before LE.
        # This is the order used by the proven manual setup flow and avoids
        # racing MAP/PBAP profile discovery against GATT discovery.
        if bredr is not True:
            self._cancel_le_settle()
        if bredr is False:
            self._request_connect("bredr")
        elif bredr is True and le is False:
            self._schedule_le_connect()
        elif le is True:
            self._cancel_le_settle()
        return True

    def _schedule_le_connect(self) -> None:
        if self._le_settle_id is not None:
            return
        log.info(
            "iPhone BR/EDR connected; allowing %ds to settle before LE",
            CLASSIC_SETTLE_SECONDS,
        )
        self._le_settle_id = self._schedule(
            CLASSIC_SETTLE_SECONDS,
            self._connect_le_after_settle,
        )

    def _connect_le_after_settle(self) -> bool:
        self._le_settle_id = None
        if not self._running:
            return False
        bredr = self._read("bredr")
        le = self._read("le")
        self._update_state("bredr", bredr)
        self._update_state("le", le)
        if bredr is True and le is False:
            self._request_connect("le")
        elif bredr is False:
            self._request_connect("bredr")
        return False

    def _cancel_le_settle(self) -> None:
        if self._le_settle_id is None:
            return
        try:
            self._cancel(self._le_settle_id)
        except Exception:
            log.debug("could not remove LE settling timer", exc_info=True)
        self._le_settle_id = None

    def _read(self, kind: str) -> bool | None:
        try:
            return self._read_connected(kind)
        except dbus.exceptions.DBusException as error:
            log.debug(
                "%s bearer state unavailable: %s",
                kind.upper(),
                error.get_dbus_name() or str(error),
            )
            return None

    def _update_state(self, kind: str, value: bool | None) -> None:
        previous = self._states[kind]
        self._states[kind] = value
        if value is True:
            self._last_errors.pop(kind, None)
        if previous != value and self._on_status is not None:
            self._on_status()

    def _request_connect(self, kind: str) -> None:
        if kind in self._connecting:
            return
        self._connecting.add(kind)
        log.info("connecting iPhone %s bearer", kind.upper())
        try:
            self._connect(
                kind,
                lambda: self._connect_succeeded(kind),
                lambda error: self._connect_failed(kind, error),
            )
        except Exception as error:
            self._connect_failed(kind, error)

    def _connect_succeeded(self, kind: str) -> None:
        self._connecting.discard(kind)
        self._last_errors.pop(kind, None)
        log.info("iPhone %s bearer connection requested successfully", kind.upper())

    def _connect_failed(self, kind: str, error: Exception) -> None:
        self._connecting.discard(kind)
        name = (
            error.get_dbus_name()
            if isinstance(error, dbus.exceptions.DBusException)
            else type(error).__name__
        ) or str(error)
        if name in {
            "org.bluez.Error.AlreadyConnected",
            "org.bluez.Error.InProgress",
        }:
            log.debug("%s bearer connection already active", kind.upper())
            return
        if self._last_errors.get(kind) != name:
            log.warning("could not connect iPhone %s bearer: %s", kind.upper(), name)
            self._last_errors[kind] = name

    def _read_bluez_connected(self, kind: str) -> bool | None:
        interface = _INTERFACES[kind]
        obj = get_system_bus().get_object("org.bluez", self.device_path)
        properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return bool(properties.Get(interface, "Connected", timeout=5.0))

    def _connect_bluez(
        self,
        kind: str,
        on_success: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        interface = _INTERFACES[kind]
        bearer = dbus.Interface(
            get_system_bus().get_object("org.bluez", self.device_path),
            interface,
        )
        bearer.Connect(
            reply_handler=on_success,
            error_handler=on_error,
            timeout=45.0,
        )
