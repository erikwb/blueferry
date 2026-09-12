"""GLib adapter for desktop activation (GTK and the standalone QS bridge)."""
from __future__ import annotations

from collections.abc import Callable

import dbus
import dbus.service

from blueferry.bus import get_session_bus
from blueferry.client_activation import (
    ACTIVATION_INTERFACE,
    ACTIVATION_PATH,
    CLIENTS,
    record_client_use,
)


class ClientActivation(dbus.service.Object):
    def __init__(self, key: str, callback: Callable[[str, str], None]) -> None:
        self._key = key
        self._callback = callback
        client = next(client for client in CLIENTS if client.key == key)
        self._name = dbus.service.BusName(
            client.bus_name, bus=get_session_bus(), do_not_queue=True,
        )
        super().__init__(self._name, ACTIVATION_PATH)

    @dbus.service.method(ACTIVATION_INTERFACE, in_signature="ss", out_signature="")
    def OpenMessage(self, handle: str, token: str) -> None:
        if len(handle) > 1024 or len(token) > 4096:
            raise dbus.DBusException("invalid activation request")
        self._callback(str(handle), str(token))
        record_client_use(self._key)

    def close(self) -> None:
        self.remove_from_connection()
        get_session_bus().release_name(self._name.get_name())
