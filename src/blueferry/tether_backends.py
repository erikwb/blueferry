"""NetworkManager and plain-BlueZ strategies for Bluetooth PAN tethering.

With NetworkManager running, BlueFerry asks it to activate a Bluetooth PAN
(``bluetooth.type=panu``) connection for the phone. NetworkManager then calls
``Network1.Connect("nap")`` itself and runs DHCP on the ``bnep`` interface,
so no process gains privileges it did not already have. An existing PAN
profile for the phone is reused untouched; only when there is none does
BlueFerry create one, per user (``connection.permissions``) and with
autoconnect off, so NetworkManager never tethers on its own because of it.

Without NetworkManager, BlueFerry calls ``Network1.Connect("nap")`` directly
and reports the interface; the user runs their own DHCP client. BlueZ ties
that link to the calling D-Bus connection, so it ends with the daemon.
"""

from __future__ import annotations

import logging
import os
import pwd
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import dbus

from blueferry.tether import (
    ACTIVATION_FAILED,
    BACKEND_BLUEZ,
    BACKEND_NETWORKMANAGER,
    BLUEZ,
    GENERIC_ERROR,
    IP_CONFIG_FAILED,
    LINK_LOST,
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
    Lost,
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
NM_BT_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device.Bluetooth"
NM_CONNECTION_IFACE = "org.freedesktop.NetworkManager.Settings.Connection"
NM_DEVICE_TYPE_BT = 5
MAX_PROFILE_CANDIDATES = 16
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

# NMActiveConnectionState
NM_ACTIVE_ACTIVATING = 1
NM_ACTIVE_ACTIVATED = 2
NM_ACTIVE_DEACTIVATING = 3
NM_ACTIVE_DEACTIVATED = 4
NM_REASON_USER_DISCONNECTED = 2
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


def _bdaddr_text(value: object) -> str:
    """NetworkManager reports ``bluetooth.bdaddr`` as bytes or as a string."""
    if isinstance(value, str):
        return value
    try:
        octets = [int(octet) for octet in value]  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return ""
    return ":".join(f"{octet:02X}" for octet in octets)


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

    def connect(self, on_connected: Connected, on_error: Failed, _on_lost: Lost) -> None:
        # Link loss is reported by the controller's Network1 watch.
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
    """Activate the phone's Bluetooth PAN profile through NetworkManager.

    An existing ``bluetooth``/``panu`` profile for the phone (for example
    one plasma-nm or nmcli created) is reused as is and never modified.
    Only when none exists does BlueFerry add its own per-user profile.
    """

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
        # UUIDs of every PAN profile that belongs to this phone; the one that
        # was activated is included once chosen.
        self._profile_uuids: set[str] = {self._uuid}
        self._profile_uuid = self._uuid
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

    def _get_all(self, path: str, interface: str, reply, error) -> None:
        self._call(path, PROPERTIES_IFACE, "GetAll", "s", (interface,), reply, error)

    def _drop_state_match(self) -> None:
        match, self._state_match = self._state_match, None
        if match is not None:
            try:
                match.remove()
            except Exception as error:
                log.debug(
                    "could not remove NetworkManager state watch: %s",
                    dbus_error_name(error),
                )

    def _same_phone(self, address: object) -> bool:
        return str(address).strip().casefold() == self._mac.casefold()

    # ---- profile resolution --------------------------------------------

    def _resolve_profile(self, generation: int, on_profile: Callable[[str | None], None]) -> None:
        """Find an existing PAN profile for this phone; ``None`` if there is none.

        Resolution never fails the attempt: any error falls back to the
        UUID lookup/creation path.
        """

        def current() -> bool:
            return generation == self._generation

        def give_up(_error: Exception | None = None) -> None:
            if current():
                on_profile(None)

        def scan_devices(paths: list[str]) -> None:
            if not current():
                return
            if not paths:
                on_profile(None)
                return
            head, rest = paths[0], paths[1:]

            def device(properties: Mapping) -> None:
                if not current():
                    return
                if int(properties.get("DeviceType", 0)) != NM_DEVICE_TYPE_BT:
                    scan_devices(rest)
                    return
                available = [str(value) for value in properties.get("AvailableConnections", [])]

                def address(value: object) -> None:
                    if not current():
                        return
                    if self._same_phone(value):
                        inspect(available[:MAX_PROFILE_CANDIDATES], [])
                    else:
                        scan_devices(rest)

                self._get(head, NM_BT_DEVICE_IFACE, "HwAddress", address,
                          lambda _error: scan_devices(rest))

            self._get_all(head, NM_DEVICE_IFACE, device, lambda _error: scan_devices(rest))

        def inspect(candidates: list[str], accepted: list[tuple[str, str, int]]) -> None:
            if not current():
                return
            if not candidates:
                choose(accepted)
                return
            head, rest = candidates[0], candidates[1:]

            def settings(value: Mapping) -> None:
                if not current():
                    return
                profile = self._pan_profile(value)
                inspect(rest, accepted + [(head, *profile)] if profile else accepted)

            self._call(head, NM_CONNECTION_IFACE, "GetSettings", "", (), settings,
                       lambda _error: inspect(rest, accepted))

        def choose(accepted: list[tuple[str, str, int]]) -> None:
            if not accepted:
                on_profile(None)
                return
            self._profile_uuids.update(uuid_ for _path, uuid_, _stamp in accepted)
            own = [entry for entry in accepted if entry[1] == self._uuid]
            chosen = own[0] if own else max(accepted, key=lambda entry: entry[2])
            self._profile_uuid = chosen[1]
            log.info(
                "reusing an existing NetworkManager PAN profile (%s)",
                "BlueFerry's" if own else "created outside BlueFerry",
            )
            on_profile(chosen[0])

        self._call(NM_PATH, NM, "GetAllDevices", "", (),
                   lambda paths: scan_devices([str(path) for path in paths][:64]),
                   give_up)

    def _pan_profile(self, settings: Mapping) -> tuple[str, int] | None:
        """``(uuid, timestamp)`` for a PAN client profile of this phone."""
        connection = settings.get("connection") or {}
        bluetooth = settings.get("bluetooth") or {}
        if str(connection.get("type", "")) != "bluetooth":
            return None
        if str(bluetooth.get("type", "")) != "panu":
            return None  # e.g. a DUN profile for the same phone
        bdaddr = bluetooth.get("bdaddr")
        if bdaddr is not None and not self._same_phone(_bdaddr_text(bdaddr)):
            return None
        uuid_ = str(connection.get("uuid", ""))
        if not uuid_:
            return None
        try:
            stamp = int(connection.get("timestamp", 0))
        except (TypeError, ValueError):
            stamp = 0
        return uuid_, stamp

    # ---- connect -------------------------------------------------------

    def connect(self, on_connected: Connected, on_error: Failed, on_lost: Lost) -> None:
        self._generation += 1
        generation = self._generation
        self._drop_state_match()
        established = False

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
            # Kept after activation too: it is how a deliberate deactivation
            # in the network applet is told apart from a lost link.
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
            nonlocal established
            if not current() or self._state_match is None:
                return
            if state == NM_ACTIVE_ACTIVATED and not established:
                established = True
                self._read_interface(generation, on_connected)
            elif state in (NM_ACTIVE_DEACTIVATING, NM_ACTIVE_DEACTIVATED):
                self._drop_state_match()
                self._active_path = None
                log.info("NetworkManager deactivated the tether (reason %d)", reason)
                if established:
                    on_lost(
                        _NM_REASON_TOKENS.get(reason, LINK_LOST),
                        reason == NM_REASON_USER_DISCONNECTED,
                    )
                else:
                    on_error(_NM_REASON_TOKENS.get(reason, ACTIVATION_FAILED))

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

        def resolved(profile_path: str | None) -> None:
            if not current():
                return
            if profile_path is not None:
                activate(profile_path)
                return
            self._profile_uuid = self._uuid
            self._call(
                NM_SETTINGS_PATH, NM_SETTINGS_IFACE, "GetConnectionByUuid", "s",
                (self._uuid,), activate, lookup_failed,
            )

        self._resolve_profile(generation, resolved)

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
        """Locate the active PAN connection for this phone.

        That is BlueFerry's own or a reused profile's UUID, or any active
        ``bluetooth`` connection whose device is this phone (a tether that
        the network applet started). Unrelated connections never match.
        """

        def current() -> bool:
            return generation == self._generation

        def failed(error: Exception) -> None:
            if current():
                on_error(nm_error_token(error))

        def check(paths: list[str]) -> None:
            if not current():
                return
            if not paths:
                on_found(None)
                return
            head, rest = paths[0], paths[1:]

            def skip(_error: Exception | None = None) -> None:
                # One vanished active connection must not hide the others.
                if current():
                    check(rest)

            def compare(properties: Mapping) -> None:
                if not current():
                    return
                if str(properties.get("Uuid", "")) in self._profile_uuids:
                    on_found(head)
                    return
                devices = [str(value) for value in properties.get("Devices", [])]
                if str(properties.get("Type", "")) != "bluetooth" or not devices:
                    skip()
                    return

                def address(value: object) -> None:
                    if not current():
                        return
                    if self._same_phone(value):
                        on_found(head)
                    else:
                        skip()

                self._get(devices[0], NM_BT_DEVICE_IFACE, "HwAddress", address, skip)

            self._get_all(head, NM_ACTIVE_IFACE, compare, skip)

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
