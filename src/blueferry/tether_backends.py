"""NetworkManager and plain-BlueZ strategies for Bluetooth PAN tethering.

With NetworkManager running, BlueFerry asks it to activate a Bluetooth PAN
(``bluetooth.type=panu``) connection for the phone. NetworkManager then calls
``Network1.Connect("nap")`` itself and runs DHCP on the ``bnep`` interface,
so no process gains privileges it did not already have. The profile is
created once, per user (``connection.permissions``), with autoconnect off, so
NetworkManager never tethers on its own.

Without NetworkManager, BlueFerry calls ``Network1.Connect("nap")`` directly
and reports the interface; the user runs their own DHCP client. BlueZ ties
that link to the calling D-Bus connection, so it ends with the daemon.
"""

from __future__ import annotations

import logging
import os
import pwd
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import dbus

from blueferry.tether import (
    ACTIVATION_FAILED,
    BACKEND_BLUEZ,
    BACKEND_NETWORKMANAGER,
    BLUEZ,
    GENERIC_ERROR,
    IP_CONFIG_FAILED,
    NETWORK_IFACE,
    NETWORKMANAGER_UNAVAILABLE,
    NO_NETWORK_DEVICE,
    NOT_SUPPORTED,
    PERMISSION_DENIED,
    PROPERTIES_IFACE,
    TIMEOUT,
    AsyncBus,
    Connected,
    Done,
    Failed,
    TetherBackend,
    bluez_error_token,
    dbus_error_name,
)

log = logging.getLogger(__name__)

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
NM_SETTINGS_IFACE = "org.freedesktop.NetworkManager.Settings"
NM_ACTIVE_IFACE = "org.freedesktop.NetworkManager.Connection.Active"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

# NMActiveConnectionState
NM_ACTIVE_ACTIVATING = 1
NM_ACTIVE_ACTIVATED = 2
NM_ACTIVE_DEACTIVATING = 3
NM_ACTIVE_DEACTIVATED = 4
# NMActiveConnectionStateReason values that deserve their own guidance.
_NM_REASON_TOKENS = {
    5: IP_CONFIG_FAILED,   # IP_CONFIG_INVALID: link up, but no DHCP lease
    6: TIMEOUT,            # CONNECT_TIMEOUT
    7: TIMEOUT,            # SERVICE_START_TIMEOUT
    14: NO_NETWORK_DEVICE,  # DEVICE_REMOVED
}

BLUEZ_CONNECT_TIMEOUT_SECONDS = 60.0
NM_CALL_TIMEOUT_SECONDS = 25.0
CONNECTION_ID = "BlueFerry iPhone hotspot"
_UUID_NAMESPACE = uuid.UUID("6f0f3a8e-6a53-4f0a-9a0e-1d0b1f0e7e11")


def connection_uuid(mac: str) -> str:
    """Stable NetworkManager profile UUID for one phone, reused on every run."""
    return str(uuid.uuid5(_UUID_NAMESPACE, f"blueferry-tether/{mac.upper()}"))


def _bdaddr_bytes(mac: str) -> dbus.Array:
    return dbus.Array(
        [dbus.Byte(int(part, 16)) for part in mac.split(":")], signature="y"
    )


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def nm_connection_settings(mac: str, user: str) -> dict[str, Any]:
    """PAN client profile: user-private, never autoconnected by NetworkManager."""
    return dbus.Dictionary({
        "connection": dbus.Dictionary({
            "id": dbus.String(CONNECTION_ID),
            "uuid": dbus.String(connection_uuid(mac)),
            "type": dbus.String("bluetooth"),
            "autoconnect": dbus.Boolean(False),
            # A user-owned profile needs only settings.modify.own, not the
            # system-wide permission, and stays invisible to other accounts.
            "permissions": dbus.Array([f"user:{user}"], signature="s"),
        }, signature="sv"),
        "bluetooth": dbus.Dictionary({
            "bdaddr": _bdaddr_bytes(mac),
            "type": dbus.String("panu"),
        }, signature="sv"),
        "ipv4": dbus.Dictionary({"method": dbus.String("auto")}, signature="sv"),
        "ipv6": dbus.Dictionary({"method": dbus.String("auto")}, signature="sv"),
    }, signature="sa{sv}")


def nm_error_token(error: object) -> str:
    name = dbus_error_name(error)
    lowered = name.casefold()
    if "permissiondenied" in lowered or "accessdenied" in lowered or lowered.endswith(
        ".notauthorized"
    ):
        return PERMISSION_DENIED
    if name in {
        f"{NM}.UnknownDevice",
        f"{NM}.ConnectionNotAvailable",
        f"{NM}.UnknownConnection",
    }:
        return NO_NETWORK_DEVICE
    if name in {
        f"{NM}.Settings.InvalidConnection",
        f"{NM}.Settings.InvalidProperty",
        f"{NM}.Settings.MissingProperty",
        f"{NM}.Settings.NotSupported",
    }:
        # NetworkManager without Bluetooth support rejects the profile.
        return NOT_SUPPORTED
    if name in {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
    }:
        return NETWORKMANAGER_UNAVAILABLE
    if name in {"org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.Timeout"}:
        return TIMEOUT
    return GENERIC_ERROR


class BluezTether:
    """Connect the PAN link only; IP configuration is left to the user."""

    name = BACKEND_BLUEZ

    def __init__(self, bus: Callable[[], AsyncBus], device_path: str) -> None:
        self._bus = bus
        self._device_path = device_path
        self._generation = 0

    def _call(self, method: str, signature: str, args: tuple, reply, error, timeout: float) -> None:
        self._bus().call_async(
            BLUEZ, self._device_path, NETWORK_IFACE, method, signature, args,
            reply, error, timeout=timeout,
        )

    def connect(self, on_connected: Connected, on_error: Failed) -> None:
        self._generation += 1
        generation = self._generation

        def reply(interface: object = "") -> None:
            if generation == self._generation:
                on_connected(str(interface))

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            name = dbus_error_name(error)
            if name == "org.bluez.Error.AlreadyConnected":
                # The link is ours already (or another tool's); report it.
                self._bus().call_async(
                    BLUEZ, self._device_path, PROPERTIES_IFACE, "Get", "ss",
                    (NETWORK_IFACE, "Interface"), reply,
                    lambda _error: reply(""), timeout=5.0,
                )
                return
            log.info("BlueZ PAN connect failed: %s", name)
            on_error(bluez_error_token(error))

        self._call("Connect", "s", ("nap",), reply, failed, BLUEZ_CONNECT_TIMEOUT_SECONDS)

    def disconnect(self, on_done: Done, on_error: Failed) -> None:
        self._generation += 1
        generation = self._generation

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            name = dbus_error_name(error)
            if name == "org.bluez.Error.NotConnected":
                on_done()
                return
            log.info("BlueZ PAN disconnect failed: %s", name)
            on_error(bluez_error_token(error))

        def done(*_args: object) -> None:
            if generation == self._generation:
                on_done()

        # Network1.Disconnect tears down BNEP only, never the shared ACL link.
        self._call("Disconnect", "", (), done, failed, 20.0)

    def cancel(self) -> None:
        self._generation += 1


class NetworkManagerTether:
    """Activate a per-user Bluetooth PAN profile through NetworkManager."""

    name = BACKEND_NETWORKMANAGER

    def __init__(
        self,
        bus: Callable[[], AsyncBus],
        mac: str,
        *,
        user: Callable[[], str] = _current_user,
    ) -> None:
        self._bus = bus
        self._mac = mac
        self._user = user
        self._uuid = connection_uuid(mac)
        self._generation = 0
        self._active_path: str | None = None
        self._state_match: Any = None

    # ---- D-Bus helpers -------------------------------------------------

    def _call(self, path: str, interface: str, method: str, signature: str,
              args: tuple, reply, error, *, bus_name: str = NM) -> None:
        self._bus().call_async(
            bus_name, path, interface, method, signature, args, reply, error,
            timeout=NM_CALL_TIMEOUT_SECONDS,
        )

    def _get(self, path: str, interface: str, prop: str, reply, error) -> None:
        self._call(path, PROPERTIES_IFACE, "Get", "ss", (interface, prop), reply, error)

    def _drop_state_match(self) -> None:
        match, self._state_match = self._state_match, None
        if match is not None:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove NetworkManager state watch", exc_info=True)

    # ---- connect -------------------------------------------------------

    def connect(self, on_connected: Connected, on_error: Failed) -> None:
        self._generation += 1
        generation = self._generation
        self._drop_state_match()

        def current() -> bool:
            return generation == self._generation

        def fail(stage: str) -> Callable[[Exception], None]:
            def handler(error: Exception) -> None:
                if current():
                    log.info("NetworkManager %s failed: %s", stage, dbus_error_name(error))
                    self._drop_state_match()
                    on_error(nm_error_token(error))
            return handler

        def activate(connection_path: object) -> None:
            if not current():
                return
            self._call(
                NM_PATH, NM, "ActivateConnection", "ooo",
                (dbus.ObjectPath(str(connection_path)), dbus.ObjectPath("/"),
                 dbus.ObjectPath("/")),
                activated, fail("activation"),
            )

        def activated(active_path: object) -> None:
            path = str(active_path)
            if not current():
                # Disconnect or a deadline overtook this reply.
                self._deactivate(path, lambda: None, lambda _token: None)
                return
            self._active_path = path
            self._state_match = self._bus().add_signal_receiver(
                lambda state, reason: observe(int(state), int(reason)),
                signal_name="StateChanged",
                dbus_interface=NM_ACTIVE_IFACE,
                bus_name=NM,
                path=path,
            )
            # The profile may already be up before the watch existed.
            self._get(path, NM_ACTIVE_IFACE, "State",
                      lambda state: observe(int(state), -1), fail("state read"))

        def observe(state: int, reason: int) -> None:
            if not current() or self._state_match is None:
                return
            if state == NM_ACTIVE_ACTIVATED:
                self._drop_state_match()
                self._read_interface(generation, on_connected)
            elif state in (NM_ACTIVE_DEACTIVATING, NM_ACTIVE_DEACTIVATED):
                self._drop_state_match()
                self._active_path = None
                token = _NM_REASON_TOKENS.get(reason, ACTIVATION_FAILED)
                log.info("NetworkManager deactivated the tether (reason %d)", reason)
                on_error(token)

        def lookup_failed(error: Exception) -> None:
            if not current():
                return
            if dbus_error_name(error) == f"{NM}.Settings.InvalidConnection":
                # No profile yet: create the user-private one once.
                self._call(
                    NM_SETTINGS_PATH, NM_SETTINGS_IFACE, "AddConnection", "a{sa{sv}}",
                    (nm_connection_settings(self._mac, self._user()),),
                    activate, fail("profile creation"),
                )
                return
            fail("profile lookup")(error)

        self._call(
            NM_SETTINGS_PATH, NM_SETTINGS_IFACE, "GetConnectionByUuid", "s",
            (self._uuid,), activate, lookup_failed,
        )

    def _read_interface(self, generation: int, on_connected: Connected) -> None:
        """Report the bnep interface name; activation already succeeded."""
        path = self._active_path

        def report(name: object = "") -> None:
            if generation == self._generation:
                on_connected(str(name))

        def unknown(_error: Exception) -> None:
            report("")

        def devices(values: Sequence[object]) -> None:
            if generation != self._generation:
                return
            if not values:
                report("")
                return
            self._get(str(values[0]), NM_DEVICE_IFACE, "IpInterface", report, unknown)

        if path is None:
            report("")
            return
        self._get(path, NM_ACTIVE_IFACE, "Devices", devices, unknown)

    # ---- disconnect ----------------------------------------------------

    def disconnect(self, on_done: Done, on_error: Failed) -> None:
        self._generation += 1
        generation = self._generation
        self._drop_state_match()

        def done() -> None:
            if generation == self._generation:
                on_done()

        def failed(token: str) -> None:
            if generation == self._generation:
                on_error(token)

        path, self._active_path = self._active_path, None
        if path is not None:
            self._deactivate(path, done, failed)
            return
        self._find_active(
            generation,
            lambda found: self._deactivate(found, done, failed) if found else done(),
            failed,
        )

    def _deactivate(self, path: str, on_done: Done, on_error: Failed) -> None:
        def failed(error: Exception) -> None:
            name = dbus_error_name(error)
            if name in {f"{NM}.ConnectionNotActive", "org.freedesktop.DBus.Error.UnknownObject"}:
                on_done()
                return
            log.info("NetworkManager deactivation failed: %s", name)
            on_error(nm_error_token(error))

        self._call(NM_PATH, NM, "DeactivateConnection", "o",
                   (dbus.ObjectPath(path),), lambda *_args: on_done(), failed)

    def _find_active(
        self, generation: int, on_found: Callable[[str | None], None], on_error: Failed,
    ) -> None:
        """Locate BlueFerry's active profile, e.g. after a daemon restart."""

        def failed(error: Exception) -> None:
            if generation == self._generation:
                on_error(nm_error_token(error))

        def check(paths: list[str]) -> None:
            if generation != self._generation:
                return
            if not paths:
                on_found(None)
                return
            head, rest = paths[0], paths[1:]

            def compare(value: object) -> None:
                if generation != self._generation:
                    return
                if str(value) == self._uuid:
                    on_found(head)
                else:
                    check(rest)

            def vanished(_error: Exception) -> None:
                # One vanished active connection must not hide the others.
                check(rest)

            self._get(head, NM_ACTIVE_IFACE, "Uuid", compare, vanished)

        def listed(values: Sequence[object]) -> None:
            check([str(value) for value in values][:64])

        self._get(NM_PATH, NM, "ActiveConnections", listed, failed)

    def cancel(self) -> None:
        """Stop reporting; keep the active path so a later Disconnect works."""
        self._generation += 1
        self._drop_state_match()


def choose_backend(
    bus: Callable[[], AsyncBus],
    device_path: str,
    mac: str,
    mode: str = "auto",
) -> Callable[[Callable[[TetherBackend], None], Failed], None]:
    """Prefer NetworkManager when it owns its bus name, BlueZ otherwise."""

    def choose(on_backend: Callable[[TetherBackend], None], on_error: Failed) -> None:
        if mode == BACKEND_NETWORKMANAGER:
            on_backend(NetworkManagerTether(bus, mac))
            return
        if mode == BACKEND_BLUEZ:
            on_backend(BluezTether(bus, device_path))
            return

        def owned(has_owner: object) -> None:
            if bool(has_owner):
                on_backend(NetworkManagerTether(bus, mac))
            else:
                on_backend(BluezTether(bus, device_path))

        def failed(error: Exception) -> None:
            log.info("could not probe for NetworkManager: %s", dbus_error_name(error))
            on_error(GENERIC_ERROR)

        bus().call_async(
            DBUS, DBUS_PATH, DBUS, "NameHasOwner", "s", (NM,), owned, failed,
            timeout=5.0,
        )

    return choose
