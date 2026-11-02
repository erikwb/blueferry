"""Hear ``Plugin1.CardChanged`` only from the enabled card plugins (GDBus).

GTK counterpart of the QtDBus watching in ``qt/plugin_surfaces.py``. The
shared :class:`blueferry.plugin_surfaces_model.PluginSurfacesModel` names
the bus names of the enabled card plugins after each load; this watcher
keeps one GDBus signal subscription per name. A subscription for a
well-known sender is matched by the bus against that name's current owner
(and GDBus checks the sender again), so no other session-bus client can
make the card call every plugin in a loop by sending ``CardChanged``.
Toolkit-free apart from Gio, so it is tested on the private test bus.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from gi.repository import Gio, GLib

from blueferry.gio_dbus import GioBus, GioDBus, Subscription
from blueferry.plugin_api import OBJECT_PATH, SIGNAL_CARD_CHANGED, SURFACES_INTERFACE

log = logging.getLogger(__name__)


def session_bus() -> Gio.DBusConnection:
    """The process-wide GDBus session connection (cached by GDBus)."""
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


class CardChangeWatcher:
    """Hears ``CardChanged`` only from the owners of the watched bus names."""

    def __init__(self, on_changed: Callable[[], None], bus: GioBus | None = None) -> None:
        self._on_changed = on_changed
        self._bus = bus
        self._subscriptions: dict[str, Subscription] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._subscriptions)

    def watch(self, names: Sequence[str]) -> None:
        wanted = {str(name) for name in names if name}
        for name in sorted(set(self._subscriptions) - wanted):
            self._subscriptions.pop(name).remove()
        missing = sorted(wanted - set(self._subscriptions))
        if not missing:
            return
        try:
            bus = self._bus = self._bus or GioDBus(session_bus)
            for name in missing:
                self._subscriptions[name] = bus.subscribe(
                    name, SURFACES_INTERFACE, SIGNAL_CARD_CHANGED,
                    lambda *_args: self._on_changed(), path=OBJECT_PATH,
                )
        except GLib.Error:
            # Without a session bus no plugin can answer either.
            log.warning("cannot watch plugin cards", exc_info=True)

    def stop(self) -> None:
        for subscription in self._subscriptions.values():
            subscription.remove()
        self._subscriptions.clear()
