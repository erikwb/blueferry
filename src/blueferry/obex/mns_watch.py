"""Watch the iPhone's MAP notification (MNS) connection.

MAP runs over two OBEX connections: our MAS session to the iPhone, and the
iPhone's MNS connection back to obexd, over which it pushes new messages.
obexd registers for notifications once, when the MAS session is created, and
never again. If the iPhone drops MNS, the MAS session keeps working while new
messages silently stop arriving. obexd publishes each accepted MNS connection
as an org.bluez.obex.Session1 object under /org/bluez/obex/server and removes
it on disconnect, so its absence is directly observable.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.bus import get_session_bus

log = logging.getLogger(__name__)

MNS_TARGET = "BB582B41-420C-11DB-B0DE-0800200C9A66"
SESSION_INTERFACE = "org.bluez.obex.Session1"
SERVER_SESSION_PREFIX = "/org/bluez/obex/server/"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
# The iPhone connects MNS within a second of MAS registration.
OPEN_GRACE_SECONDS = 30
# Let a MAS loss that took MNS with it reach the profile supervisor first.
LOSS_GRACE_SECONDS = 5

Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


class MnsWatch:
    """Report when the iPhone has no MNS connection to obexd."""

    def __init__(
        self,
        phone_mac: str,
        *,
        on_missing: Callable[[str], None],
        on_present: Callable[[], None],
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
    ) -> None:
        self.phone_mac = phone_mac.upper()
        self._on_missing = on_missing
        self._on_present = on_present
        self._schedule = schedule
        self._cancel = cancel
        self._sessions: set[str] = set()
        self._matches: list = []
        self._timer_id: int | None = None
        self._running = False

    @property
    def connected(self) -> bool:
        return bool(self._sessions)

    def start(self) -> None:
        bus = get_session_bus()
        self._running = True
        # Subscribe before the inventory so a connection cannot fall between.
        self._matches = [
            bus.add_signal_receiver(
                self._on_interfaces_added,
                dbus_interface=OBJECT_MANAGER,
                signal_name="InterfacesAdded",
                bus_name="org.bluez.obex",
            ),
            bus.add_signal_receiver(
                self._on_interfaces_removed,
                dbus_interface=OBJECT_MANAGER,
                signal_name="InterfacesRemoved",
                bus_name="org.bluez.obex",
            ),
        ]
        try:
            objects = dbus.Interface(
                bus.get_object("org.bluez.obex", "/"), OBJECT_MANAGER,
            ).GetManagedObjects(timeout=5.0)
        except dbus.exceptions.DBusException:
            log.debug("could not list obexd sessions", exc_info=True)
            objects = {}
        for path, interfaces in objects.items():
            self._on_interfaces_added(path, interfaces)
        if not self._sessions:
            self._arm(OPEN_GRACE_SECONDS, "the iPhone did not open MAP notifications")

    def stop(self) -> None:
        self._running = False
        self._disarm()
        for match in self._matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove MNS watch", exc_info=True)
        self._matches = []
        self._sessions.clear()

    def _on_interfaces_added(self, path, interfaces) -> None:
        path_s = str(path)
        props = interfaces.get(SESSION_INTERFACE)
        if (
            not self._running
            or props is None
            or not path_s.startswith(SERVER_SESSION_PREFIX)
            or str(props.get("Target", "")).upper() != MNS_TARGET
            or str(props.get("Destination", "")).upper() != self.phone_mac
        ):
            return
        log.info("iPhone MAP notifications connected (%s)", path_s)
        self._sessions.add(path_s)
        self._disarm()
        self._on_present()

    def _on_interfaces_removed(self, path, interfaces) -> None:
        path_s = str(path)
        if (
            not self._running
            or path_s not in self._sessions
            or SESSION_INTERFACE not in interfaces
        ):
            return
        self._sessions.discard(path_s)
        log.warning("iPhone MAP notifications disconnected (%s)", path_s)
        if not self._sessions:
            self._arm(LOSS_GRACE_SECONDS, "the iPhone closed MAP notifications")

    def _arm(self, delay: int, reason: str) -> None:
        self._disarm()
        self._timer_id = self._schedule(delay, lambda: self._expired(reason))

    def _disarm(self) -> None:
        if self._timer_id is None:
            return
        try:
            self._cancel(self._timer_id)
        except Exception:
            log.debug("could not remove MNS watch timer", exc_info=True)
        self._timer_id = None

    def _expired(self, reason: str) -> bool:
        self._timer_id = None
        if self._running and not self._sessions:
            self._on_missing(reason)
        return False
