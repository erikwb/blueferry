"""Bounded, privacy-preserving diagnostics for pairing attempts."""
from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from blueferry import quirks_report
from blueferry.ancs.constants import ANCS_CHAR_UUIDS, ANCS_SERVICE_UUID
from blueferry.bluetooth_devices import PairedDevice
from blueferry.pairing_types import PairingAttempt, PairingTransports

BLUEZ_SNAPSHOT_TIMEOUT_SECONDS = 5.0

log = logging.getLogger(__name__)

ManagedObjects = Mapping[object, Mapping[str, Mapping[str, Any]]]


def bluez_device_snapshot(
    device_path: str,
    *,
    load_objects: Callable[[float], ManagedObjects],
) -> dict[str, object]:
    """Return a public, bounded snapshot of one BlueZ device subtree.

    Object paths and addresses are intentionally omitted. The report needs
    structural and connection state, not the phone's Bluetooth identity.
    """
    snapshot: dict[str, object] = {
        "object_present": False,
        "device_present": False,
        "child_objects": 0,
        "root_interfaces": [],
        "child_interfaces": {},
        "device": {
            "paired": False,
            "trusted": False,
            "connected": False,
            "services_resolved": False,
            "ancs_uuid": False,
            "uuid_count": 0,
        },
        "bearers": {
            "bredr": {"present": False},
            "le": {"present": False},
        },
        "gatt": {
            "services": 0,
            "characteristics": 0,
            "ancs_service": False,
            "ancs_characteristics": [],
        },
        "battery_objects": 0,
    }
    try:
        objects = load_objects(BLUEZ_SNAPSHOT_TIMEOUT_SECONDS)
    except Exception as error:
        snapshot["error"] = type(error).__name__
        return snapshot

    root_ifaces: Mapping[str, Mapping[str, Any]] = {}
    subtree: list[tuple[str, Mapping[str, Mapping[str, Any]]]] = []
    prefix = f"{device_path}/"
    for raw_path, raw_ifaces in objects.items():
        path = str(raw_path)
        if path == device_path:
            root_ifaces = raw_ifaces
        if path == device_path or path.startswith(prefix):
            subtree.append((path, raw_ifaces))

    snapshot["object_present"] = bool(root_ifaces)
    snapshot["child_objects"] = sum(path != device_path for path, _ in subtree)
    snapshot["root_interfaces"] = sorted(str(name) for name in root_ifaces)
    child_interfaces: Counter[str] = Counter()
    service_uuids: set[str] = set()
    characteristic_uuids: set[str] = set()
    battery_objects = 0
    for path, ifaces in subtree:
        if path != device_path:
            child_interfaces.update(str(name) for name in ifaces)
        if "org.bluez.Battery1" in ifaces:
            battery_objects += 1
        service = ifaces.get("org.bluez.GattService1")
        if service is not None:
            uuid = str(service.get("UUID", "")).casefold()
            if uuid:
                service_uuids.add(uuid)
        characteristic = ifaces.get("org.bluez.GattCharacteristic1")
        if characteristic is not None:
            uuid = str(characteristic.get("UUID", "")).casefold()
            if uuid:
                characteristic_uuids.add(uuid)

    device = root_ifaces.get("org.bluez.Device1")
    snapshot["device_present"] = device is not None
    if device is not None:
        uuids = sorted({str(value).casefold() for value in device.get("UUIDs", ())})
        snapshot["device"] = {
            "paired": bool(device.get("Paired", False)),
            "trusted": bool(device.get("Trusted", False)),
            "connected": bool(device.get("Connected", False)),
            "services_resolved": bool(device.get("ServicesResolved", False)),
            "ancs_uuid": ANCS_SERVICE_UUID in uuids,
            "uuid_count": len(uuids),
        }

    bearers: dict[str, dict[str, object]] = {}
    for label, interface in (
        ("bredr", "org.bluez.Bearer.BREDR1"),
        ("le", "org.bluez.Bearer.LE1"),
    ):
        bearer = root_ifaces.get(interface)
        state: dict[str, object] = {"present": bool(bearer)}
        if bearer:
            for field in ("Paired", "Bonded", "Connected"):
                if field in bearer:
                    state[field.casefold()] = bool(bearer[field])
        bearers[label] = state
    snapshot["bearers"] = bearers
    snapshot["child_interfaces"] = dict(sorted(child_interfaces.items()))
    snapshot["battery_objects"] = battery_objects
    snapshot["gatt"] = {
        "services": child_interfaces["org.bluez.GattService1"],
        "characteristics": child_interfaces["org.bluez.GattCharacteristic1"],
        "ancs_service": ANCS_SERVICE_UUID in service_uuids,
        "ancs_characteristics": sorted(ANCS_CHAR_UUIDS & characteristic_uuids),
    }
    return snapshot


def record_bluez_state(
    attempt: PairingAttempt | None,
    device_path: str | None,
    phase: str,
    *,
    snapshot: Callable[[str], dict[str, object]],
    monotonic: Callable[[], float] = time.monotonic,
    force: bool = False,
) -> None:
    """Append a BlueZ snapshot when the device subtree changes."""
    if attempt is None or not device_path:
        return
    state = snapshot(device_path)
    trace = attempt.setdefault("bluez_trace", [])
    if not isinstance(trace, list):
        return
    if trace and not force and trace[-1].get("state") == state:
        return
    origin = attempt.get("_t0")
    elapsed = (
        round(monotonic() - origin, 3)
        if isinstance(origin, (int, float))
        else 0.0
    )
    trace.append({"t": elapsed, "phase": phase, "state": state})
    # Retain the initial state and the most recent transitions under churn.
    if len(trace) > 32:
        del trace[1]
    log.debug("BlueZ device state (%s): %s", phase, state)


def le_bearer_snapshot(
    device_path: str,
    *,
    load_objects: Callable[[], ManagedObjects],
) -> dict[str, object]:
    state: dict[str, object] = {
        "present": False,
        "paired": False,
        "bonded": False,
        "connected": False,
    }
    try:
        objects = load_objects()
        interfaces = next(
            (value for path, value in objects.items() if str(path) == device_path),
            {},
        )
        bearer = interfaces.get("org.bluez.Bearer.LE1") if interfaces else None
        # Without -E, bluetoothd still registers an empty Bearer.LE1.
        if not bearer:
            return state
        state["present"] = True
        state["paired"] = bool(bearer.get("Paired", False))
        state["bonded"] = bool(bearer.get("Bonded", False))
        state["connected"] = bool(bearer.get("Connected", False))
    except Exception:
        log.debug("could not inspect LE bearer", exc_info=True)
    return state


def remember_daemon_status(attempt: PairingAttempt | None, status: object) -> None:
    if attempt is None:
        return
    extra = getattr(status, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    subscribed = bool(extra.get("ancs_subscribed"))
    authorized = bool(extra.get("ancs_authorized"))
    last_le_error = str(extra.get("last_le_error") or "")[:256]
    last_le_message = str(extra.get("last_le_error_message") or "")[:256]
    previous = attempt.get("daemon")
    if (
        not last_le_error
        and extra.get("le") is not True
        and isinstance(previous, dict)
    ):
        last_le_error = str(previous.get("last_le_error") or "")[:256]
        if not last_le_message:
            last_le_message = str(previous.get("last_le_error_message") or "")[:256]
    daemon = {
        "ancs_subscribed": subscribed,
        "ancs_authorized": authorized,
        "bredr": extra.get("bredr"),
        "le": extra.get("le"),
    }
    if last_le_error:
        daemon["last_le_error"] = last_le_error
        if last_le_message:
            daemon["last_le_error_message"] = last_le_message
    attempt["daemon"] = daemon
    phone = attempt.get("phone")
    if isinstance(phone, dict) and last_le_error:
        bearer = phone.setdefault("le_bearer", {})
        if isinstance(bearer, dict):
            bearer["last_error"] = last_le_error
            if last_le_message:
                bearer["last_error_message"] = last_le_message
    seen = {
        item.get("event")
        for item in attempt.get("timeline", ())
        if isinstance(item, dict)
    }
    if subscribed and "ancs_subscribed" not in seen:
        quirks_report.mark(attempt, "ancs_subscribed")
    if authorized and "ancs_authorized" not in seen:
        quirks_report.mark(attempt, "ancs_authorized")
    if last_le_error and "le_connect_failed" not in seen:
        fields: dict[str, str] = {"error": last_le_error}
        if last_le_message:
            fields["message"] = last_le_message
        quirks_report.mark(attempt, "le_connect_failed", **fields)


def snapshot_phone(
    attempt: PairingAttempt,
    device: PairedDevice,
    *,
    bearer_snapshot: Callable[[str], dict[str, object]],
) -> None:
    phone = quirks_report.public_device(device)
    phone["le_bearer"] = bearer_snapshot(device.device_path)
    name = str(getattr(device, "name", "") or "").strip()
    if name:
        aliases = attempt.setdefault("_aliases", [])
        if isinstance(aliases, list) and name not in aliases:
            aliases.append(name)
    previous = attempt.get("phone")
    if isinstance(previous, dict):
        previous_bearer = previous.get("le_bearer")
        if isinstance(previous_bearer, dict) and previous_bearer.get("last_error"):
            phone["le_bearer"]["last_error"] = previous_bearer["last_error"]
            if previous_bearer.get("last_error_message"):
                phone["le_bearer"]["last_error_message"] = previous_bearer[
                    "last_error_message"
                ]
    attempt["phone"] = phone


def device_bonded(attempt: PairingAttempt) -> bool:
    """True once Device1.Paired is observed, even if later setup fails."""
    phone = attempt.get("phone")
    if isinstance(phone, dict) and phone.get("paired") is True:
        return True
    for item in attempt.get("timeline", ()):
        if not isinstance(item, dict):
            continue
        if item.get("event") == "paired":
            return True
        if item.get("event") == "device_loaded" and item.get("already_paired"):
            return True
    return False


def pairing_outcome(
    attempt: PairingAttempt,
    transports: PairingTransports | None,
    error: Exception | None,
) -> dict[str, object]:
    """Record each profile separately; MAP-only is not a single degraded bit."""
    outcome: dict[str, object] = {
        "bonded": device_bonded(attempt),
        "setup_complete": error is None and transports is not None,
        "map": None,
        "pbap": None,
        "ancs": None,
    }
    daemon = attempt.get("daemon")
    if isinstance(daemon, dict):
        if "ancs_subscribed" in daemon:
            outcome["ancs_subscribed"] = daemon["ancs_subscribed"]
        if "ancs_authorized" in daemon:
            outcome["ancs_authorized"] = daemon["ancs_authorized"]
        if daemon.get("last_le_error"):
            outcome["last_le_error"] = daemon["last_le_error"]
        if daemon.get("last_le_error_message"):
            outcome["last_le_error_message"] = daemon["last_le_error_message"]
    if transports is not None:
        outcome["map"], outcome["pbap"], outcome["ancs"] = transports.as_tuple()
    else:
        events = {
            item.get("event")
            for item in attempt.get("timeline", ())
            if isinstance(item, dict)
        }
        if "map_ready" in events:
            outcome["map"] = True
        if "pbap_ready" in events:
            outcome["pbap"] = True
        if "ancs_ready" in events:
            outcome["ancs"] = True
    if error is not None:
        outcome["error"] = str(error)[:1024]
    return outcome


def record_pairing_report(
    attempt: PairingAttempt,
    *,
    transports: PairingTransports | None = None,
    error: Exception | None = None,
) -> Path | None:
    if transports is not None:
        map_ready, pbap_ready, ancs_ready = transports.as_tuple()
        attempt["preferred_bearer"] = "le"
        seen = {
            item.get("event")
            for item in attempt.get("timeline", ())
            if isinstance(item, dict)
        }
        if map_ready and "map_ready" not in seen:
            quirks_report.mark(attempt, "map_ready")
        if pbap_ready and "pbap_ready" not in seen:
            quirks_report.mark(attempt, "pbap_ready")
        if ancs_ready and "ancs_ready" not in seen:
            quirks_report.mark(attempt, "ancs_ready")
    attempt["outcome"] = pairing_outcome(attempt, transports, error)
    if error is not None:
        quirks_report.mark(attempt, "failed")
    elif transports is not None:
        quirks_report.mark(attempt, "finished")
    else:
        quirks_report.mark(attempt, "failed")
    return quirks_report.save_report(attempt)
