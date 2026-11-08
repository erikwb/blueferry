"""Hear ``Plugin1.CardChanged`` only from the enabled card plugins (dbus-python).

For the clients that use dbus-python with a GLib main loop: the terminal
client and the Quickshell bridge. The GTK client has the GDBus twin in
``ui/card_watch.py``; Qt watches with QtDBus. The shared
:class:`blueferry.plugin_surfaces_model.PluginSurfacesModel` names the bus
names of the enabled card plugins after each load; this watcher keeps one
match per name. dbus-python matches a well-known ``bus_name`` against its
current owner, so no other session-bus client can make a client call every
plugin in a loop by sending ``CardChanged``. Signals arrive while the
default GLib context runs, on its thread; call :meth:`watch` and
:meth:`stop` there too.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from blueferry.plugin_api import OBJECT_PATH, SIGNAL_CARD_CHANGED, SURFACES_INTERFACE

log = logging.getLogger(__name__)


def _session_bus() -> Any:
    from blueferry.bus import get_session_bus

    return get_session_bus()


class CardChangeWatcher:
    """Hears ``CardChanged`` only from the owners of the watched bus names."""

    def __init__(
        self, on_changed: Callable[[], None], bus: Callable[[], Any] = _session_bus,
    ) -> None:
        self._on_changed = on_changed
        self._bus = bus
        self._matches: dict[str, Any] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._matches)

    def watch(self, names: Sequence[str]) -> None:
        wanted = {str(name) for name in names if name}
        for name in sorted(set(self._matches) - wanted):
            self._remove(self._matches.pop(name))
        missing = sorted(wanted - set(self._matches))
        if not missing:
            return
        try:
            bus = self._bus()
            for name in missing:
                self._matches[name] = bus.add_signal_receiver(
                    lambda *_args: self._on_changed(),
                    signal_name=SIGNAL_CARD_CHANGED, dbus_interface=SURFACES_INTERFACE,
                    bus_name=name, path=OBJECT_PATH,
                )
        except Exception:  # without a session bus no plugin can answer either
            log.warning("cannot watch plugin cards", exc_info=True)

    @staticmethod
    def _remove(match: Any) -> None:
        try:
            match.remove()
        except Exception:
            log.debug("cannot remove a CardChanged match", exc_info=True)

    def stop(self) -> None:
        for match in self._matches.values():
            self._remove(match)
        self._matches.clear()
