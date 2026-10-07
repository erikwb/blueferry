"""BlueZ adapter preparation for the pairing flow in PROTOCOL.md.

For MAP/PBAP to be reachable on iOS 26.5, three things must be true on
the Linux side:

1. Adapter Class-of-Device set to A/V Hands-Free (Major=4 Minor=8).
2. A connectable, discoverable BLE peripheral advert is active with
   SolicitUUIDs containing the ANCS UUID.  The small manufacturer/service
   payloads keep the advertisement visible to iOS during first pairing.
3. Adapter is powered.

This module owns those three concerns. Class-of-Device changes run through a
hardened, argument-validating systemd service; its fixed operation is available
to an active local daemon so volatile controller state can be repaired after a
Bluetooth reset without granting raw Bluetooth capabilities to BlueFerry.
"""
from __future__ import annotations

import itertools
import logging
import os
import time
from typing import Any

import dbus
import dbus.exceptions
import dbus.service
from gi.repository import GLib

from blueferry import config
from blueferry.bus import bluez, get_system_bus
from blueferry.commands import run_command
from blueferry.errors import CommandError, PairingError

log = logging.getLogger(__name__)

ADVERT_ACTIVATION_TIMEOUT_SECONDS = 15
ADVERT_POLL_INTERVAL_SECONDS = 0.05
PAIRING_ADVERT_SETTLE_SECONDS = 5
POLKIT_UNAVAILABLE_MESSAGE = (
    "No Polkit authentication is available to set device class, "
    "please use the blueferry pair-setup command from a terminal"
)
DEVICE_CLASS_SERVICE_MISSING_MESSAGE = (
    "The BlueFerry device-class service is not installed; "
    "install blueferry-backend before pairing."
)
_POLKIT_UNAVAILABLE_MARKERS = (
    "interactive authentication required",
    "no authentication agent",
)


# ---- Class-of-Device ----------------------------------------------------

def current_cod(adapter: str | None = None) -> int | None:
    """Return adapter Class field, or None if unavailable."""
    adapter = adapter or config.ADAPTER
    try:
        v = bluez(f"/org/bluez/{adapter}",
                  "org.freedesktop.DBus.Properties").Get(
            "org.bluez.Adapter1", "Class")
        return int(v)
    except dbus.exceptions.DBusException:
        return None


def desired_cod_matches(cod: int | None) -> bool:
    """Major & Minor match what we want? Service-class bits are derived
    by BlueZ from registered profiles, so we only compare the low 16 bits
    of (Major<<8 | Minor<<2)."""
    if cod is None:
        return False
    major = (cod >> 8) & 0x1F
    minor = (cod >> 2) & 0x3F
    return major == config.COD_MAJOR and (minor << 2) == config.COD_MINOR


def _polkit_authentication_unavailable(output: str) -> bool:
    normalized = output.casefold()
    return any(marker in normalized for marker in _POLKIT_UNAVAILABLE_MARKERS)


def set_cod(
    *,
    adapter: str | None = None,
    authorize: bool = False,
    dry_run: bool = False,
) -> bool:
    """Apply the required CoD, optionally requesting system authorization."""
    adapter = adapter or config.ADAPTER
    if not config.is_valid_adapter(adapter):
        log.error("invalid Bluetooth adapter name: %s", adapter)
        return False
    index = adapter.removeprefix("hci")
    cmd = [
        "/usr/bin/btmgmt", "--index", index, "class",
        str(config.COD_MAJOR), str(config.COD_MINOR),
    ]
    if os.geteuid() != 0:
        if not authorize:
            log.warning("adapter CoD differs; pairing setup must authorize the change")
            return False
        systemctl = "/usr/bin/systemctl"
        if not os.path.isfile(systemctl) or not os.access(systemctl, os.X_OK):
            log.error("systemctl is unavailable; cannot authorize adapter setup")
            return False
        cmd = [
            systemctl,
            "start",
            f"blueferry-btmgmt-set-class@{index}.service",
        ]
    log.info("setting adapter CoD via: %s", " ".join(cmd))
    if dry_run:
        return True
    try:
        r = run_command(
            cmd,
            timeout=120 if authorize else 10,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except CommandError as e:
        log.error("btmgmt failed: %s", e)
        return False
    if r.returncode != 0:
        output = r.stderr.strip() or r.stdout.strip()
        if cmd[0] == "/usr/bin/systemctl":
            if _polkit_authentication_unavailable(output):
                raise PairingError(POLKIT_UNAVAILABLE_MESSAGE)
            normalized = output.casefold()
            if (
                "blueferry-btmgmt-set-class@" in normalized
                and "not found" in normalized
            ):
                raise PairingError(DEVICE_CLASS_SERVICE_MISSING_MESSAGE)
        log.error("btmgmt class %d %d failed (rc=%d): %s",
                  config.COD_MAJOR, config.COD_MINOR, r.returncode,
                  output)
        return False
    log.info("CoD set ok: %s", r.stdout.strip())
    return True


# ---- BLE advertisement (SolicitUUIDs = ANCS) ----------------------------

class _AncsAdvert(dbus.service.Object):
    """LEAdvertisement1 object shaped like a real ANCS accessory.

    SolicitUUIDs is the meaningful protocol field.  The otherwise inert
    manufacturer/service payloads mirror the working ancs4linux pairing flow:
    some iOS/controller combinations ignored the solicitation-only advert
    during first pairing.  0xffff and 0x9999 are explicitly test/private IDs;
    they do not impersonate a hardware vendor or service.
    """

    PATH = config.BLE_ADVERT_DBUS_PATH

    def __init__(self, adapter: str, path: str) -> None:
        bus = get_system_bus()
        self._bus = bus
        # Pin the proxy to this BlueZ owner. A late cleanup must never reach
        # a replacement bluetoothd. Explicit signatures avoid introspection.
        self.manager = dbus.Interface(
            bus.get_object("org.bluez", f"/org/bluez/{adapter}", introspect=False),
            "org.bluez.LEAdvertisingManager1",
        )
        super().__init__(bus, path)
        self.adapter = adapter
        self.path = path
        self.registered = False
        self.pending = False
        self.retired = False
        self.request: Any = None

    def start(self) -> None:
        self.pending = True
        try:
            # Proxy methods discard call_async's PendingCall. Use the
            # connection API so retirement can really cancel reply dispatch,
            # while retaining the proxy's pinned BlueZ owner for both calls.
            self.request = self._bus.call_async(
                bus_name=self.manager.bus_name,
                object_path=f"/org/bluez/{self.adapter}",
                dbus_interface="org.bluez.LEAdvertisingManager1",
                method="RegisterAdvertisement",
                signature="oa{sv}",
                args=(dbus.ObjectPath(self.path), dbus.Dictionary({}, signature="sv")),
                reply_handler=self._registered,
                error_handler=self._failed,
                timeout=float(ADVERT_ACTIVATION_TIMEOUT_SECONDS),
            )
        except dbus.exceptions.DBusException as error:
            self._failed(error)

    def _registered(self) -> None:
        if self.retired or not self.pending:
            return
        self.pending = False
        self.registered = True
        self.request = None
        log.info("BLE advert registered: %s", self.path)

    def _failed(self, error: dbus.exceptions.DBusException) -> None:
        if self.retired:
            return
        self.request = None
        log.warning("RegisterAdvertisement failed: %s: %s",
                    error.get_dbus_name(), error.get_dbus_message())
        # NoReply and AlreadyExists are not activation proof. Retire this
        # unique path so an unresolved request cannot poison future retries.
        self.retire()

    def retire(self, *, unregister: bool = True) -> None:
        if self.retired:
            return
        self.retired = True
        self.pending = self.registered = False
        if self.request is not None:
            self.request.cancel()
            self.request = None
        if unregister:
            try:
                self.manager.UnregisterAdvertisement(
                    dbus.ObjectPath(self.path), signature="o",
                    reply_handler=lambda: None,
                    error_handler=lambda error: log.debug(
                        "UnregisterAdvertisement: %s", error.get_dbus_name()),
                    timeout=5.0,
                )
            except dbus.exceptions.DBusException:
                log.debug("could not unregister retired advert", exc_info=True)
        self.remove_from_connection()

    @dbus.service.method("org.bluez.LEAdvertisement1",
                         in_signature="", out_signature="")
    def Release(self) -> None:
        self.retire(unregister=False)
        log.info("BlueZ released the ANCS solicitation advertisement")
        return None

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface: str) -> dict[str, Any]:
        if iface != "org.bluez.LEAdvertisement1":
            raise dbus.exceptions.DBusException(
                f"Unknown interface {iface}",
                name="org.freedesktop.DBus.Error.InvalidArgs")
        return {
            "Type": dbus.String("peripheral"),
            "SolicitUUIDs": dbus.Array([config.ANCS_SOLICIT_UUID], signature="s"),
            "ManufacturerData": dbus.Dictionary({
                dbus.UInt16(0xFFFF): dbus.Array(
                    [dbus.Byte(v) for v in (0x50, 0xB0, 0x13, 0xF0)],
                    signature="y", variant_level=1,
                ),
            }, signature="qv"),
            "ServiceData": dbus.Dictionary({
                "00009999-0000-1000-8000-00805f9b34fb": dbus.Array(
                    [dbus.Byte(v) for v in (0x9E, 0x85, 0x39, 0x96)],
                    signature="y", variant_level=1,
                ),
            }, signature="sv"),
            # Scoped to this advertisement rather than making the whole
            # adapter permanently discoverable.  Three minutes is enough for
            # a deliberate first-pair operation; ANCS solicitation remains
            # present after the discoverable flag expires.
            "Discoverable": dbus.Boolean(True),
            "DiscoverableTimeout": dbus.UInt16(180),
            "LocalName": dbus.String(config.BLE_ADVERT_LOCAL_NAME),
            "Includes": dbus.Array(["tx-power"], signature="s"),
        }

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface: str, prop: str):
        return self.GetAll(iface)[prop]


_advert_instance: _AncsAdvert | None = None
_advert_serial = itertools.count(1)


def advert_registered() -> bool:
    """Return whether the current BlueZ owner accepted our advertisement."""
    return bool(_advert_instance and _advert_instance.registered)


def advert_registration_pending() -> bool:
    return bool(_advert_instance and _advert_instance.pending)


def forget_advert_registration() -> None:
    """Discard registration state that belonged to a departed BlueZ owner."""
    global _advert_instance
    previous, _advert_instance = _advert_instance, None
    if previous is not None:
        previous.retire(unregister=False)


def register_advert(
    adapter: str | None = None,
    *,
    settle_for_pairing: bool = False,
) -> bool:
    """Start registration; only a successful BlueZ reply means active.

    Daemon callers return immediately and let GLib dispatch both BlueZ's
    GetAll request and the eventual reply. Pairing callers wait with dispatch
    enabled, then leave a settling interval before handing off to the daemon.
    ActiveInstances includes *pending* BlueZ registrations and proves nothing
    about whether a particular advertisement reached the controller.
    """
    global _advert_instance
    adapter = adapter or config.ADAPTER
    if not config.is_valid_adapter(adapter):
        raise ValueError("invalid Bluetooth adapter name")
    current = _advert_instance
    if current is not None and current.adapter != adapter:
        current.retire()
    if current is None or current.retired:
        # BlueZ keys registrations by sender + object path. Never reuse a
        # path that could still have a pending controller completion.
        try:
            current = _AncsAdvert(adapter, f"{_AncsAdvert.PATH}/r{next(_advert_serial)}")
        except dbus.exceptions.DBusException:
            log.warning("could not access the BlueZ advertising manager", exc_info=True)
            return False
        _advert_instance = current
        current.start()
    if not settle_for_pairing:
        return current.registered

    context = GLib.MainContext.default()
    deadline = time.monotonic() + ADVERT_ACTIVATION_TIMEOUT_SECONDS
    while current.pending and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(ADVERT_POLL_INTERVAL_SECONDS)
    if current.pending:
        log.warning("timed out waiting for ANCS advertisement registration")
        current.retire()
    if not current.registered:
        return False
    deadline = time.monotonic() + PAIRING_ADVERT_SETTLE_SECONDS
    while current.registered and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(ADVERT_POLL_INTERVAL_SECONDS)
    return current.registered


def unregister_advert(adapter: str | None = None) -> None:
    """Best-effort unregister; safe to call on shutdown."""
    global _advert_instance
    adapter = adapter or config.ADAPTER
    current = _advert_instance
    if current is not None and current.adapter == adapter:
        _advert_instance = None
        current.retire()


def prepare_classic(*, adapter: str | None = None, authorize: bool = False) -> bool:
    """Prepare only the BR/EDR identity, without exposing the LE advert.

    Advertising ANCS solicitation while Classic pairing is still in flight
    lets the iPhone connect the unbonded LE peripheral as a separate device,
    leaving two accessory records on the phone. Pairing therefore starts from
    the classic identity alone; the advert is registered only after the bond
    exists.
    """
    adapter = adapter or config.ADAPTER
    cod = current_cod(adapter)
    log.info("current adapter Class = 0x%06x", cod or 0)
    if desired_cod_matches(cod):
        log.info("CoD already matches A/V Hands-Free, leaving as-is")
        return True
    return set_cod(adapter=adapter, authorize=authorize)
