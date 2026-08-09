"""BlueZ adapter preparation for the pairing flow in PROTOCOL.md.

For MAP/PBAP to be reachable on iOS 26.5, three things must be true on
the Linux side:

1. Adapter Class-of-Device set to A/V Hands-Free (Major=4 Minor=8).
2. A connectable, discoverable BLE peripheral advert is active with
   SolicitUUIDs containing the ANCS UUID.  The small manufacturer/service
   payloads keep the advertisement visible to iOS during first pairing.
3. Adapter is powered.

This module owns those three concerns. The daemon may verify them on startup,
but privileged Class-of-Device changes are limited to the explicit pairing
flow and authorized through Polkit.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import dbus
import dbus.exceptions
import dbus.service

from blueferry import config
from blueferry.bus import bluez, get_system_bus
from blueferry.commands import run_command
from blueferry.errors import CommandError

log = logging.getLogger(__name__)

ADVERT_DBUS_TIMEOUT_SECONDS = 1
ADVERT_ACTIVATION_TIMEOUT_SECONDS = 15
ADVERT_POLL_INTERVAL_SECONDS = 0.25


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


def set_cod(
    *,
    adapter: str | None = None,
    authorize: bool = False,
    dry_run: bool = False,
) -> bool:
    """Apply the required CoD, optionally requesting Polkit authorization."""
    adapter = adapter or config.ADAPTER
    cmd = [
        "/usr/bin/btmgmt", "--index", adapter.removeprefix("hci"), "class",
        str(config.COD_MAJOR), str(config.COD_MINOR),
    ]
    if os.geteuid() != 0:
        if not authorize:
            log.warning("adapter CoD differs; pairing setup must authorize the change")
            return False
        pkexec = "/usr/bin/pkexec"
        if not os.path.isfile(pkexec) or not os.access(pkexec, os.X_OK):
            log.error("pkexec is unavailable; cannot authorize adapter setup")
            return False
        cmd = [pkexec, *cmd]
    log.info("setting adapter CoD via: %s", " ".join(cmd))
    if dry_run:
        return True
    try:
        r = run_command(
            cmd, timeout=120 if authorize else 10, check=False
        )
    except CommandError as e:
        log.error("btmgmt failed: %s", e)
        return False
    if r.returncode != 0:
        log.error("btmgmt class %d %d failed (rc=%d): %s",
                  config.COD_MAJOR, config.COD_MINOR, r.returncode,
                  r.stderr.strip() or r.stdout.strip())
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

    @dbus.service.method("org.bluez.LEAdvertisement1",
                         in_signature="", out_signature="")
    def Release(self) -> None:
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
_advert_registered = False


def _active_advertisements(adapter: str | None = None) -> int | None:
    """Return BlueZ's active-advert count, or None if it is unavailable."""
    adapter = adapter or config.ADAPTER
    try:
        value = dbus.Interface(
            get_system_bus().get_object(
                "org.bluez", f"/org/bluez/{adapter}"
            ),
            "org.freedesktop.DBus.Properties",
        ).Get("org.bluez.LEAdvertisingManager1", "ActiveInstances")
        return int(value)
    except dbus.exceptions.DBusException:
        return None


def register_advert(adapter: str | None = None) -> bool:
    """Register the BLE advertisement on the system bus.

    Idempotent — calling twice is harmless because BlueZ will reject the
    second registration and we treat that as success.
    """
    global _advert_instance, _advert_registered
    adapter = adapter or config.ADAPTER
    if _advert_instance is None:
        _advert_instance = _AncsAdvert(get_system_bus(), _AncsAdvert.PATH)

    ad_mgr = bluez(f"/org/bluez/{adapter}",
                   "org.bluez.LEAdvertisingManager1")
    active_before = _active_advertisements(adapter)
    activation_deadline = time.monotonic() + ADVERT_ACTIVATION_TIMEOUT_SECONDS
    try:
        # Hardware-offloaded advertisements on the MediaTek controller can
        # activate without BlueZ replying to this call. Use a short D-Bus
        # timeout, then observe the actual controller state instead of making
        # pairing wait for a reply that may never arrive.
        ad_mgr.RegisterAdvertisement(
            _AncsAdvert.PATH,
            {},
            timeout=float(ADVERT_DBUS_TIMEOUT_SECONDS),
        )
        _advert_registered = True
        log.info("BLE advert registered: %s", _AncsAdvert.PATH)
        return True
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name()
        if name == "org.bluez.Error.AlreadyExists":
            _advert_registered = True
            log.info("BLE advert already registered")
            return True
        if name == "org.freedesktop.DBus.Error.NoReply":
            while True:
                # Only claim this registration succeeded if the count
                # increased. `count > 0` can mistake an unrelated or stale
                # advertisement for ours.
                active_after = _active_advertisements(adapter)
                if (active_before is not None and active_after is not None
                        and active_after > active_before):
                    _advert_registered = True
                    log.info("BLE advert activated after delayed reply "
                             "(ActiveInstances=%d→%d)",
                             active_before, active_after)
                    return True
                remaining = activation_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(ADVERT_POLL_INTERVAL_SECONDS, remaining))
        log.error("RegisterAdvertisement failed: %s: %s",
                  name, e.get_dbus_message())
        return False


def unregister_advert(adapter: str | None = None) -> None:
    """Best-effort unregister; safe to call on shutdown."""
    global _advert_registered
    adapter = adapter or config.ADAPTER
    try:
        ad_mgr = bluez(f"/org/bluez/{adapter}",
                       "org.bluez.LEAdvertisingManager1")
        ad_mgr.UnregisterAdvertisement(_AncsAdvert.PATH)
    except dbus.exceptions.DBusException as e:
        log.debug("UnregisterAdvertisement: %s", e.get_dbus_name())
    finally:
        _advert_registered = False


# ---- one-shot startup ---------------------------------------------------

def prepare(*, adapter: str | None = None, authorize: bool = False) -> bool:
    """Run all the prerequisites. Returns False if anything critical failed.

    Idempotent. Safe to call on every daemon start.
    """
    ok = True
    adapter = adapter or config.ADAPTER
    cod = current_cod(adapter)
    log.info("current adapter Class = 0x%06x", cod or 0)
    if not desired_cod_matches(cod):
        ok &= set_cod(adapter=adapter, authorize=authorize)
    else:
        log.info("CoD already matches A/V Hands-Free, leaving as-is")

    ok &= register_advert(adapter)
    return ok
