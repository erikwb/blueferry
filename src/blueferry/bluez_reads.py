"""Bounded asynchronous BlueZ observations for main-loop health checks."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import dbus

from blueferry.bus import get_system_bus


class BluezReadCache:
    """Coalesce reads; never return stale evidence after an error or timeout."""
    def __init__(self, bus: Callable = get_system_bus, clock: Callable = time.monotonic):
        self._bus = bus
        self._clock = clock
        self._generation = 0
        self._entries: dict[tuple, tuple[float, Any, Exception | None]] = {}
        self._pending: dict[tuple, object] = {}

    def invalidate(self) -> None:
        self._generation += 1
        self._entries.clear()
        self._pending.clear()

    def invalidate_managed_objects(self) -> None:
        for key in self._entries.keys() | self._pending.keys():
            if key[3] == "GetManagedObjects":
                self._entries.pop(key, None)
                self._pending.pop(key, None)

    def properties_changed(self, path, interface, changed, invalidated) -> None:
        for name in set(changed) | set(invalidated):
            key = ("org.bluez", path, "org.freedesktop.DBus.Properties",
                   "Get", "ss", (interface, name))
            # Ignore unrelated properties (especially notification payloads).
            if key not in self._entries and key not in self._pending:
                continue
            self._pending.pop(key, None)
            self._entries.pop(key, None)
            if name in changed:
                self._entries[key] = (self._clock(), changed[name], None)

    def read(self, path: str, interface: str, method: str, signature: str,
             args: tuple, *, owner: str = "org.bluez",
             on_ready: Callable[[], None] | None = None) -> Any:
        key = (owner, path, interface, method, signature, args)
        now = self._clock()
        entry = self._entries.get(key)
        if key not in self._pending and (entry is None or now - entry[0] >= 5):
            generation = self._generation
            token = object()
            self._pending[key] = token

            def finish(value=None, error=None):
                if generation != self._generation or self._pending.get(key) is not token:
                    return
                self._pending.pop(key, None)
                self._entries[key] = (self._clock(), value, error)
                if error is None and on_ready is not None:
                    on_ready()

            try:
                self._bus().call_async(
                    owner, path, interface, method, signature, args,
                    lambda value: finish(value), lambda error: finish(error=error),
                    timeout=5.0,
                )
            except Exception as error:
                finish(error=error)
            entry = self._entries.get(key)
        if entry is None or now - entry[0] > 10:
            raise dbus.exceptions.DBusException(
                'BlueZ observation pending', name='org.freedesktop.DBus.Error.NoReply')
        if entry[2] is not None:
            raise entry[2]
        return entry[1]

    def property(self, path: str, interface: str, name: str, *,
                 on_ready: Callable[[], None] | None = None) -> Any:
        return self.read(path, 'org.freedesktop.DBus.Properties', 'Get', 'ss',
                         (interface, name), on_ready=on_ready)


observations = BluezReadCache()
