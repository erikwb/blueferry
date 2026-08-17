"""Bluetooth discovery, pairing, and first-run configuration."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry import bluetooth_capabilities as capabilities
from blueferry import config, pairing_diagnostics, quirks_report
from blueferry.bearer_supervisor import preferred_bearer_unavailable
from blueferry.bluetooth_devices import PairedDevice
from blueferry.bus import get_session_bus, get_system_bus
from blueferry.commands import run_command
from blueferry.errors import CommandError, PairingError
from blueferry.pairing_policy import PairingPolicy, resolve_pairing_policy
from blueferry.pairing_types import PairingAttempt, PairingOutcome, PairingTransports
from blueferry.private_files import atomic_write_private_text, read_private_text
from blueferry.setup_verification import clear_setup_verification

log = logging.getLogger(__name__)

LOCAL_ENV_PATH = config.LOCAL_ENV_PATH
_SESSION_BLUETOOTH_NAMES = (
    "org.blueman.Applet",
    "org.kde.BlueDevil.Client",
    "org.kde.BlueDevil.Service",
    "org.kde.kded5",
    "org.kde.kded6",
    "org.gnome.SettingsDaemon.Rfkill",
)
CLASSIC_SETTLE_SECONDS = 3.0
BLUEZ_SNAPSHOT_TIMEOUT_SECONDS = pairing_diagnostics.BLUEZ_SNAPSHOT_TIMEOUT_SECONDS
BLUEZ_TRACE_POLL_SECONDS = 2.0
# One BR/EDR inquiry is 10.24s. USB dongles also need a moment to start
# after another adapter is stopped; 8s was aborting mid-inquiry.
DISCOVERY_SECONDS = 24
MAX_DISCOVERY_SECONDS = 30

ConfirmationCallback = Callable[[int | None], bool]
DisplayCallback = Callable[[int], None]


class _TransportStatus(Protocol):
    @property
    def map(self) -> bool: ...

    @property
    def pbap(self) -> bool: ...

    @property
    def ancs(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _PairingPreparation:
    device: PairedDevice
    adapter: str
    policy: PairingPolicy


@dataclass(slots=True)
class _PairingResources:
    agents: ExitStack = field(default_factory=ExitStack)
    advert_registered: bool = False
    agent_registered: bool = False


# Quickshell runs forget and pair in separate helper processes.
_pending_teardown_traces: dict[str, dict[str, object]] = {}
_TEARDOWN_TRACE_FILE = "pairing-teardown.json"
_TEARDOWN_TRACE_MAX_BYTES = 16 * 1024
_TEARDOWN_TRACE_MAX_AGE_SECONDS = 60 * 60


def configuration_status() -> dict:
    """Return first-run state without activating the user daemon."""
    values = config.read_local_env(LOCAL_ENV_PATH)
    mac = values.get("BLUEFERRY_MAC", "").upper()
    saved = bool(config.is_valid_mac(mac) and mac != "AA:BB:CC:DD:EE:FF")
    configured_adapter = values.get("BLUEFERRY_ADAPTER", "")
    adapter = configured_adapter if config.is_valid_adapter(configured_adapter) else config.ADAPTER
    bonded = bond_status(mac, adapter) if saved else False
    # A temporary BlueZ failure should not erase a valid setup decision. A
    # definitive, reachable adapter with no bond means setup is incomplete.
    configured = saved and bonded is not False
    return {
        "configured": configured,
        "saved": saved,
        "bonded": bonded,
        "mac": mac if saved else "",
        "adapter": adapter,
        "ancs_enabled": (
            values.get("BLUEFERRY_ANCS_ENABLED", "true").casefold()
            not in {"0", "false", "no", "off"}
        ),
        "path": str(LOCAL_ENV_PATH),
    }


def bluetooth_compatibility(adapter_name: str | None = None) -> dict:
    """Describe whether a controller supports BlueFerry's transports.

    MAP and PBAP require BR/EDR. The proven ANCS pairing flow additionally
    requires LE advertising and secure pairing. This check is read-only and
    intentionally uses ``btmgmt info`` rather than vendor/model allowlists.
    """
    if adapter_name is not None and not config.is_valid_adapter(adapter_name):
        raise PairingError("invalid Bluetooth adapter name")
    return capabilities.compatibility(
        adapter_name or config.ADAPTER,
        adapter_name=adapter_name,
        object_manager=_object_manager,
        run_command=run_command,
        support_status=bluez_support_status,
    )


def bluez_support_status(*, proc_root: Path = Path("/proc")) -> dict:
    """Report whether the packaged experimental bearer API is active."""
    return capabilities.bluez_support_status(
        run_command=run_command,
        proc_root=proc_root,
    )


def activate_bluez_support() -> dict:
    """Restart Bluetooth via systemd so the packaged drop-in takes effect."""
    return capabilities.activate_bluez_support(
        status=bluez_support_status,
        run_command=run_command,
    )


def _object_manager():
    return dbus.Interface(
        get_system_bus().get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )


def _bluez_device_snapshot(device_path: str) -> dict[str, object]:
    return pairing_diagnostics.bluez_device_snapshot(
        device_path,
        load_objects=lambda timeout: _object_manager().GetManagedObjects(
            timeout=timeout,
        ),
    )


def _record_bluez_state(
    attempt: PairingAttempt | None,
    device_path: str | None,
    phase: str,
    *,
    force: bool = False,
) -> None:
    pairing_diagnostics.record_bluez_state(
        attempt,
        device_path,
        phase,
        snapshot=_bluez_device_snapshot,
        monotonic=time.monotonic,
        force=force,
    )


def _teardown_trace_path() -> Path:
    return config.STATE_DIR / _TEARDOWN_TRACE_FILE


def _save_pending_teardown(adapter: str, trace: dict[str, object]) -> None:
    """Retain teardown diagnostics across Quickshell helper processes."""
    _pending_teardown_traces[adapter] = trace
    payload = json.dumps(
        {
            "adapter": adapter,
            "recorded_at": time.time(),
            "trace": trace,
        },
        sort_keys=True,
    )
    try:
        atomic_write_private_text(
            _teardown_trace_path(),
            payload,
            maximum_bytes=_TEARDOWN_TRACE_MAX_BYTES,
        )
    except (OSError, ValueError):
        log.debug("could not persist BlueZ teardown trace", exc_info=True)


def _take_pending_teardown(adapter: str) -> dict[str, object] | None:
    """Consume a recent teardown trace for the adapter being paired."""
    trace = _pending_teardown_traces.pop(adapter, None)
    path = _teardown_trace_path()
    if trace is None:
        try:
            payload = json.loads(
                read_private_text(path, maximum_bytes=_TEARDOWN_TRACE_MAX_BYTES)
            )
            age = time.time() - float(payload.get("recorded_at", 0))
            candidate = payload.get("trace")
            if (
                payload.get("adapter") == adapter
                and 0 <= age <= _TEARDOWN_TRACE_MAX_AGE_SECONDS
                and isinstance(candidate, dict)
            ):
                trace = candidate
                trace["capture_age_s"] = round(age, 3)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            log.debug("could not load BlueZ teardown trace", exc_info=True)
    if trace is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.debug("could not consume BlueZ teardown trace", exc_info=True)
    return trace


def bond_status(mac: str, adapter: str) -> bool | None:
    """Return paired state, or ``None`` if the adapter cannot be inspected.

    Merely retaining a MAC in ``local.env`` is not proof of a usable setup:
    package removal deliberately preserves that file while a user may remove
    the BlueZ bond independently.
    """
    adapter_path = f"/org/bluez/{adapter}"
    device_path = f"{adapter_path}/dev_{mac.replace(':', '_')}"
    try:
        managed = _object_manager().GetManagedObjects()
    except dbus.exceptions.DBusException:
        return None
    adapter_ifaces = managed.get(dbus.ObjectPath(adapter_path), managed.get(adapter_path))
    if adapter_ifaces is None or "org.bluez.Adapter1" not in adapter_ifaces:
        return None
    device_ifaces = managed.get(dbus.ObjectPath(device_path), managed.get(device_path, {}))
    device = device_ifaces.get("org.bluez.Device1") if device_ifaces else None
    return bool(device and device.get("Paired", False))


def list_devices(*, paired_only: bool = False) -> list[PairedDevice]:
    out: list[PairedDevice] = []
    for path, ifaces in _object_manager().GetManagedObjects().items():
        d = ifaces.get("org.bluez.Device1")
        if d is None:
            continue
        paired = bool(d.get("Paired", False))
        if paired_only and not paired:
            continue
        out.append(
            PairedDevice(
                mac=str(d.get("Address", "")),
                name=str(d.get("Alias") or d.get("Name") or "(unnamed)"),
                icon=str(d.get("Icon", "")),
                trusted=bool(d.get("Trusted", False)),
                connected=bool(d.get("Connected", False)),
                paired=paired,
                adapter_path=path.rsplit("/", 1)[0],
                device_path=str(path),
                uuids=frozenset(str(v).lower() for v in d.get("UUIDs", [])),
                services_resolved=bool(d.get("ServicesResolved", False)),
            )
        )
    return sorted(
        out,
        key=lambda device: (
            not device.likely_iphone,
            not device.paired,
            device.name.casefold(),
            device.mac,
        ),
    )


def _adapter_paths(objects: dict) -> list[str]:
    return [
        str(path) for path, ifaces in objects.items() if "org.bluez.Adapter1" in ifaces
    ]


def _resolve_adapter_path(
    adapters: list[str], preferred: str, *, required: bool,
) -> str:
    match = next((path for path in adapters if path.endswith(f"/{preferred}")), None)
    if match is not None:
        return match
    if required:
        raise PairingError(f"Bluetooth adapter {preferred} is not available")
    if adapters:
        return adapters[0]
    return f"/org/bluez/{preferred}"


def _stop_discovery(path: str) -> None:
    try:
        radio = dbus.Interface(
            get_system_bus().get_object("org.bluez", path),
            "org.bluez.Adapter1",
        )
        radio.StopDiscovery()
    except dbus.exceptions.DBusException as error:
        log.debug("Could not stop discovery on %s: %s", path, error)


def _on_adapter(device: PairedDevice, adapter: str) -> bool:
    return device.adapter_path.endswith(f"/{adapter}")


def discover_devices(
    seconds: int = DISCOVERY_SECONDS, *, adapter: str | None = None,
) -> list[PairedDevice]:
    """Scan through BlueZ and return devices seen on the selected adapter."""
    seconds = max(1, min(int(seconds), MAX_DISCOVERY_SECONDS))
    if adapter:
        if not config.is_valid_adapter(adapter):
            raise PairingError("invalid Bluetooth adapter name")
        preferred = adapter
        required = True
    else:
        preferred = config.ADAPTER
        required = False
    adapter_path = f"/org/bluez/{preferred}"
    selected = preferred
    discovered: list[PairedDevice] = []
    radio = None
    try:
        adapters = _adapter_paths(_object_manager().GetManagedObjects())
        adapter_path = _resolve_adapter_path(adapters, preferred, required=required)
        selected = adapter_path.rsplit("/", 1)[-1]
        bus = get_system_bus()
        for other in adapters:
            if other != adapter_path:
                _stop_discovery(other)
        obj = bus.get_object("org.bluez", adapter_path)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        radio = dbus.Interface(obj, "org.bluez.Adapter1")
        props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        try:
            radio.StartDiscovery()
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() != "org.bluez.Error.InProgress":
                raise
        deadline = time.monotonic() + seconds
        while True:
            discovered = list_devices()
            if any(
                device.likely_iphone and _on_adapter(device, selected)
                for device in discovered
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))
    except dbus.exceptions.DBusException as error:
        raise PairingError(
            error.get_dbus_message() or error.get_dbus_name() or str(error)
        ) from error
    finally:
        if radio is not None:
            try:
                radio.StopDiscovery()
            except dbus.exceptions.DBusException:
                pass
    return [device for device in discovered if _on_adapter(device, selected)]


def trust_device(mac: str, adapter_path: str) -> None:
    dev_path = f"{adapter_path}/dev_{mac.replace(':', '_')}"
    props = dbus.Interface(
        get_system_bus().get_object("org.bluez", dev_path),
        "org.freedesktop.DBus.Properties",
    )
    try:
        props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
    except dbus.exceptions.DBusException as error:
        detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
        raise PairingError(f"Could not trust the paired iPhone: {detail}") from error


def _requested_adapter(adapter: str | None) -> str:
    value = (adapter or "").strip()
    if not value:
        return ""
    if not config.is_valid_adapter(value):
        raise PairingError("invalid Bluetooth adapter name")
    return value


def _find_device(mac: str, *, adapter: str | None = None) -> PairedDevice | None:
    normalized = mac.strip().upper()
    selected = _requested_adapter(adapter)
    matches = [
        device for device in list_devices() if device.mac.upper() == normalized
    ]
    if selected:
        return next(
            (device for device in matches if _on_adapter(device, selected)),
            None,
        )
    adapters = {device.adapter_path for device in matches}
    if len(adapters) > 1:
        raise PairingError(
            "This iPhone is present on more than one Bluetooth controller; "
            "select a controller and pair again"
        )
    return matches[0] if matches else None


def _device(mac: str, *, adapter: str | None = None) -> PairedDevice:
    normalized = mac.strip().upper()
    device = _find_device(normalized, adapter=adapter)
    if device is not None:
        return device
    raise PairingError(f"Bluetooth device {normalized} is no longer available; scan again")


def _wait_for_paired_device(
    mac: str, *, adapter: str | None = None, timeout: float = 120,
) -> PairedDevice:
    """Wait for a local or iPhone-initiated pairing transaction to settle."""
    log.debug("waiting up to %.0fs for the %s pairing record to settle", timeout, mac)
    deadline = time.monotonic() + timeout
    device = _device(mac, adapter=adapter)
    while not device.paired and time.monotonic() < deadline:
        time.sleep(0.25)
        device = _device(mac, adapter=adapter)
    if not device.paired:
        raise PairingError(
            "Bluetooth pairing did not finish. Keep the iPhone unlocked with "
            "Bluetooth settings open, then retry."
        )
    log.debug("pairing record settled for %s", device.device_path)
    return device


def _dispatching_wait(deadline: float, done: Callable[[], bool]) -> bool:
    """Poll ``done`` while dispatching pending GLib events.

    The pairing agent's callbacks arrive over the same GLib default context;
    a plain ``time.sleep`` loop here would silently drop an iPhone-initiated
    confirmation that races these waits.
    """
    context = GLib.MainContext.default()
    while True:
        while context.pending():
            context.iteration(False)
        if done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _wait_for_classic_settled(
    device_path: str,
    *,
    timeout: float = 45.0,
    settle_seconds: float = CLASSIC_SETTLE_SECONDS,
    attempt: PairingAttempt | None = None,
) -> None:
    """Require an observably stable Classic connection before starting LE."""
    props = dbus.Interface(
        get_system_bus().get_object("org.bluez", device_path),
        "org.freedesktop.DBus.Properties",
    )
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    connected_since: float | None = None
    previous_connected: bool | None = None
    log.debug(
        "probing Classic connection state for up to %.0fs (%.1fs stable required)",
        timeout,
        settle_seconds,
    )
    while True:
        while context.pending():
            context.iteration(False)
        now = time.monotonic()
        try:
            connected = bool(
                props.Get("org.bluez.Device1", "Connected", timeout=5.0)
            )
        except dbus.exceptions.DBusException:
            connected = False
        if connected != previous_connected:
            log.debug(
                "Classic connection state: %s",
                "connected" if connected else "disconnected",
            )
            previous_connected = connected
            if connected:
                quirks_report.mark(attempt, "classic_connected")
        if connected:
            if connected_since is None:
                connected_since = now
            if now - connected_since >= settle_seconds:
                log.info("Classic connection is settled")
                quirks_report.mark(attempt, "classic_settled")
                return
        else:
            connected_since = None
        if now >= deadline:
            raise PairingError(
                "The iPhone's Classic Bluetooth connection did not settle "
                "before notification setup."
            )
        time.sleep(0.1)


def _connect_classic(
    device_path: str,
    *,
    timeout: float = 45.0,
    settle: bool = True,
    attempt: PairingAttempt | None = None,
) -> None:
    """Connect the Classic bearer without blocking agent dispatch."""
    log.info("sending Device1.Connect for Classic bearer: %s", device_path)
    quirks_report.mark(attempt, "classic_connect_sent")
    interface = dbus.Interface(
        get_system_bus().get_object("org.bluez", device_path),
        "org.bluez.Device1",
    )
    results: list[dbus.exceptions.DBusException | None] = []
    interface.Connect(
        reply_handler=lambda: results.append(None),
        error_handler=results.append,
        timeout=timeout,
    )
    _dispatching_wait(time.monotonic() + timeout + 5.0, lambda: bool(results))
    error = results[0] if results else None
    if error is not None and error.get_dbus_name() not in {
        "org.bluez.Error.AlreadyConnected",
        "org.bluez.Error.InProgress",
    }:
        quirks_report.mark(
            attempt, "classic_connect_failed",
            error=error.get_dbus_name() or "failed",
        )
        raise PairingError(
            error.get_dbus_message() or error.get_dbus_name() or str(error)
        ) from error
    if error is None:
        log.debug("Device1.Connect completed successfully")
        quirks_report.mark(attempt, "classic_connect_replied")
    else:
        log.debug("Device1.Connect reports %s", error.get_dbus_name())
        quirks_report.mark(
            attempt, "classic_connect_replied",
            result=error.get_dbus_name() or "error",
        )
    if settle:
        _wait_for_classic_settled(device_path, timeout=timeout, attempt=attempt)


def _prefer_bredr(device_path: str) -> None:
    """Prefer BR/EDR after pairing when BlueZ exposes PreferredBearer."""
    log.info(
        "setting Device1.PreferredBearer=bredr before post-pair connection"
    )
    obj = get_system_bus().get_object("org.bluez", device_path)
    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
    try:
        props.Set(
            "org.bluez.Device1",
            "PreferredBearer",
            dbus.String("bredr"),
        )
    except dbus.exceptions.DBusException as error:
        if preferred_bearer_unavailable(error):
            log.info(
                "Device1.PreferredBearer is unavailable; "
                "continuing with the existing Classic connection"
            )
            return
        detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
        raise PairingError(
            f"Could not select the BR/EDR bearer for the post-pair "
            f"connection: {detail}"
        ) from error


def _activate_obex_mns() -> None:
    """Activate obexd so its local MAP notification service is advertised.

    BlueZ's obexd publishes the Message Notification Server SDP record when
    it starts.  Activating it before pairing gives iOS the same stable MAP
    capability presentation used by automotive accessories instead of
    waiting for BlueFerry's daemon to start after pairing.
    """
    log.info("activating BlueZ OBEX/MNS service before pairing")
    try:
        peer = dbus.Interface(
            get_session_bus().get_object("org.bluez.obex", "/org/bluez/obex"),
            "org.freedesktop.DBus.Peer",
        )
        peer.Ping(timeout=10.0)
    except dbus.exceptions.DBusException as error:
        detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
        raise PairingError(
            f"Could not activate BlueZ's MAP notification service: {detail}"
        ) from error
    log.info("BlueZ OBEX/MNS service is available during pairing")


def _wait_for_daemon_transports(
    *,
    timeout: float = 45.0,
    attempt: PairingAttempt | None = None,
    notifications_supported: bool = True,
    device_path: str | None = None,
    status_reader: Callable[[], _TransportStatus] | None = None,
) -> PairingTransports:
    """Observe the daemon while the pairing advert and agent remain active."""
    from blueferry.client import BackendClient, BackendError

    reader: Callable[[], _TransportStatus]
    if status_reader is None:
        reader = BackendClient().status
    else:
        reader = status_reader
    deadline = time.monotonic() + timeout
    next_bluez_snapshot = 0.0
    previous: PairingTransports | None = None
    quirks_report.mark(attempt, "waiting_for_transports")
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_bluez_snapshot:
            _record_bluez_state(attempt, device_path, "transport_wait")
            next_bluez_snapshot = now + BLUEZ_TRACE_POLL_SECONDS
        try:
            status = reader()
        except BackendError:
            time.sleep(0.5)
            continue
        _remember_daemon_status(attempt, status)
        current = PairingTransports(status.map, status.pbap, status.ancs)
        if current != previous:
            log.debug(
                "daemon transport state: MAP=%s PBAP=%s ANCS=%s",
                *current.as_tuple(),
            )
            if attempt is not None:
                was = previous or PairingTransports()
                if current.map and not was.map:
                    quirks_report.mark(attempt, "map_ready")
                if current.pbap and not was.pbap:
                    quirks_report.mark(attempt, "pbap_ready")
                if current.ancs and not was.ancs:
                    quirks_report.mark(attempt, "ancs_ready")
            previous = current
        if status.ancs or (
            not notifications_supported and status.map and status.pbap
        ):
            return current
        time.sleep(0.5)
    return previous or PairingTransports()


def _restart_user_service() -> None:
    for command in (
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "restart", "blueferry.service"],
    ):
        try:
            run_command(command, timeout=30)
        except CommandError as error:
            raise PairingError(f"Could not run {' '.join(command)}: {error}") from error


def _stop_user_service() -> None:
    try:
        run_command(
            ["/usr/bin/systemctl", "--user", "stop", "blueferry.service"],
            timeout=30,
        )
    except CommandError as error:
        raise PairingError(f"Could not stop BlueFerry: {error}") from error


def complete_pairing(
    mac: str,
    *,
    adapter: str | None = None,
    confirmation: ConfirmationCallback | None = None,
    display: DisplayCallback | None = None,
    compatibility_mode: bool = False,
    explicit_pairing: bool = False,
    _allow_headless: bool = False,
) -> PairingOutcome:
    """Pair if needed and finish every Linux-side BlueFerry setup step."""
    if confirmation is None and not _allow_headless:
        raise PairingError("Pairing requires an interactive confirmation callback")
    attempt = quirks_report.start_attempt(interactive=confirmation is not None)
    attempt["pairing_options"] = {
        "compatibility_mode": bool(compatibility_mode),
        "explicit_pairing": bool(explicit_pairing),
    }
    try:
        outcome = _execute_pairing(
            mac,
            adapter=adapter,
            confirmation=confirmation,
            display=display,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
            attempt=attempt,
        )
    except PairingError as error:
        path = _record_pairing_report(attempt, error=error)
        if path is not None:
            error.report_path = str(path)
        raise
    except dbus.exceptions.DBusException as error:
        detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
        pairing_error = PairingError(detail)
        path = _record_pairing_report(attempt, error=pairing_error)
        if path is not None:
            pairing_error.report_path = str(path)
        raise pairing_error from error
    except Exception as error:
        path = _record_pairing_report(attempt, error=error)
        log.exception("unexpected pairing failure; report=%s", path or "unavailable")
        raise
    path = _record_pairing_report(
        attempt, transports=outcome.transports,
    )
    if path is not None:
        outcome = outcome.with_quirks_report(str(path))
    return outcome


def _adapter_identity(adapter: str, compatibility: dict | None = None) -> dict:
    """Build controller identity without repeating the pairing ``btmgmt`` probe."""
    extras = compatibility if isinstance(compatibility, dict) else {}
    identity = capabilities.controller_hardware(adapter)
    manufacturer_id = extras.get("manufacturer_id", identity.get("manufacturer_id"))
    if isinstance(manufacturer_id, int):
        capabilities.apply_company_id(identity, manufacturer_id)
    elif "manufacturer_id" not in identity:
        try:
            props = dbus.Interface(
                get_system_bus().get_object("org.bluez", f"/org/bluez/{adapter}"),
                "org.freedesktop.DBus.Properties",
            )
            capabilities.apply_company_id(
                identity,
                int(props.Get("org.bluez.Adapter1", "Manufacturer", timeout=5.0)),
            )
        except Exception:
            log.debug("could not read adapter manufacturer", exc_info=True)
    hci_version = extras.get("hci_version")
    if isinstance(hci_version, int):
        identity["hci_version"] = hci_version
    return identity


def _le_bearer_snapshot(device_path: str) -> dict[str, object]:
    return pairing_diagnostics.le_bearer_snapshot(
        device_path,
        load_objects=lambda: _object_manager().GetManagedObjects(),
    )


_COMPATIBILITY_REPORT_KEYS = (
    "available",
    "powered",
    "classic",
    "low_energy",
    "advertising",
    "secure_pairing",
    "secure_conn",
    "hardware_supported",
    "messages_supported",
    "notifications_supported",
    "bearer_api_supported",
    "bearer_api_active",
    "supported_settings",
    "current_settings",
    "manufacturer_id",
    "hci_version",
)


def _compatibility_fields(compatibility: dict) -> dict:
    return {
        key: compatibility[key]
        for key in _COMPATIBILITY_REPORT_KEYS
        if key in compatibility
    }


def _bluetooth_uuid(code: int) -> str:
    return f"0000{code:04x}-0000-1000-8000-00805f9b34fb"


_LOCAL_SERVICE_NAMES = {
    _bluetooth_uuid(0x1101): "serial-port",
    _bluetooth_uuid(0x1105): "obex-object-push",
    _bluetooth_uuid(0x1106): "obex-file-transfer",
    _bluetooth_uuid(0x1108): "headset",
    _bluetooth_uuid(0x110A): "audio-source",
    _bluetooth_uuid(0x110B): "audio-sink",
    _bluetooth_uuid(0x110C): "avrcp-target",
    _bluetooth_uuid(0x110E): "avrcp",
    _bluetooth_uuid(0x1112): "headset-audio-gateway",
    _bluetooth_uuid(0x111E): "handsfree",
    _bluetooth_uuid(0x111F): "handsfree-audio-gateway",
    _bluetooth_uuid(0x112E): "phonebook-access-client",
    _bluetooth_uuid(0x112F): "phonebook-access-server",
    _bluetooth_uuid(0x1132): "message-access-server",
    _bluetooth_uuid(0x1133): "message-notification-server",
    _bluetooth_uuid(0x1134): "message-access-client",
    _bluetooth_uuid(0x1200): "pnp-information",
    _bluetooth_uuid(0x1800): "generic-access",
    _bluetooth_uuid(0x1801): "generic-attribute",
    _bluetooth_uuid(0x180A): "device-information",
}

# Kernel experimental features published as Adapter1.ExperimentalFeatures.
# These are not the same as bluetoothd -E D-Bus APIs such as PreferredBearer.
_EXPERIMENTAL_FEATURE_NAMES = {
    "d4992530-b9ec-469f-ab01-6c481c47da1c": "debug",
    "671b10b5-42c0-4696-9227-eb28d1b049d6": "simultaneous-central-peripheral",
    "15c0a148-c273-11ea-b3de-0242ac130004": "ll-privacy",
    "330859bc-7506-492d-9370-9a6f0614037f": "bluetooth-quality-report",
    "a6695ace-ee7f-4fb9-881a-5fac66c629af": "offload-codecs",
    "6fbaf188-05e0-496a-9885-d6ddfdb4e03e": "iso-socket",
}


def _decode_uuid_list(value: object, names: dict[str, str]) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    decoded: list[str] = []
    seen: set[str] = set()
    for item in value:
        uuid = str(item).strip().casefold()
        if not uuid:
            continue
        label = names.get(uuid, uuid)
        if label in seen:
            continue
        seen.add(label)
        decoded.append(label)
    decoded.sort()
    return decoded


def _cod_handsfree(cod: int) -> bool:
    major = (cod >> 8) & 0x1F
    minor = (cod >> 2) & 0x3F
    return major == config.COD_MAJOR and (minor << 2) == config.COD_MINOR


def _adapter_dbus_fields(adapter: str) -> dict[str, object]:
    """Read Adapter1 Class, local UUIDs, and kernel experimental features."""
    if not config.is_valid_adapter(adapter):
        return {}
    try:
        props = dbus.Interface(
            get_system_bus().get_object("org.bluez", f"/org/bluez/{adapter}"),
            "org.freedesktop.DBus.Properties",
        )
        values = dict(props.GetAll("org.bluez.Adapter1", timeout=5.0))
    except Exception:
        log.debug("could not read adapter D-Bus properties", exc_info=True)
        return {}
    fields: dict[str, object] = {}
    raw_class = values.get("Class")
    if raw_class is not None:
        try:
            cod = int(raw_class)
        except (TypeError, ValueError):
            pass
        else:
            fields["cod"] = f"0x{cod:06x}"
            fields["cod_handsfree"] = _cod_handsfree(cod)
    if "UUIDs" in values:
        fields["uuids"] = _decode_uuid_list(values.get("UUIDs"), _LOCAL_SERVICE_NAMES)
    if "ExperimentalFeatures" in values:
        fields["experimental_features"] = _decode_uuid_list(
            values.get("ExperimentalFeatures"), _EXPERIMENTAL_FEATURE_NAMES,
        )
    return fields


def _controller_snapshot(adapter: str, compatibility: dict) -> dict:
    controller = _adapter_identity(adapter, compatibility)
    controller.update(_compatibility_fields(compatibility))
    if compatibility.get("bluez_version"):
        controller["bluez_version"] = compatibility["bluez_version"]
        controller["experimental"] = bool(
            compatibility.get("experimental")
        )
    else:
        try:
            controller.update(
                capabilities.bluez_stack(
                    run_command=run_command,
                    experimental=compatibility.get("bearer_api_active"),
                    timeout=2,
                )
            )
        except Exception:
            log.debug("could not inspect the BlueZ stack", exc_info=True)
    controller.update(_adapter_dbus_fields(adapter))
    return controller


def _bluetooth_session_owners() -> list[str]:
    """Return claimed session-bus names of desktop Bluetooth UIs."""
    try:
        bus = get_session_bus()
        daemon = dbus.Interface(
            bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "org.freedesktop.DBus",
        )
    except Exception:
        log.debug("session bus unavailable for Bluetooth owner probe", exc_info=True)
        return []
    owners: list[str] = []
    for name in _SESSION_BLUETOOTH_NAMES:
        try:
            owned = bool(daemon.NameHasOwner(name))
        except dbus.exceptions.DBusException:
            log.debug("could not query session bus name %s", name, exc_info=True)
            owned = False
        if owned:
            owners.append(name)
    return owners


def _remember_daemon_status(attempt: PairingAttempt | None, status: object) -> None:
    pairing_diagnostics.remember_daemon_status(attempt, status)


def _snapshot_phone(attempt: PairingAttempt, device: PairedDevice) -> None:
    pairing_diagnostics.snapshot_phone(
        attempt,
        device,
        bearer_snapshot=_le_bearer_snapshot,
    )


def _record_pairing_report(
    attempt: PairingAttempt,
    *,
    transports: PairingTransports | None = None,
    error: Exception | None = None,
):
    return pairing_diagnostics.record_pairing_report(
        attempt,
        transports=transports,
        error=error,
    )


def _device_bonded(attempt: PairingAttempt) -> bool:
    return pairing_diagnostics.device_bonded(attempt)


def _pairing_outcome(
    attempt: PairingAttempt,
    transports: PairingTransports | None,
    error: Exception | None,
) -> dict[str, object]:
    return pairing_diagnostics.pairing_outcome(attempt, transports, error)


def _prepare_pairing(
    mac: str,
    *,
    adapter: str | None,
    compatibility_mode: bool,
    explicit_pairing: bool,
    interactive: bool,
    attempt: PairingAttempt,
) -> _PairingPreparation:
    requested = _requested_adapter(adapter)
    device = _device(mac, adapter=requested or None)
    selected_adapter = requested or device.adapter_path.rsplit("/", 1)[-1]
    log.info(
        "starting setup for %s (%s) on %s: paired=%s trusted=%s connected=%s",
        device.name,
        device.mac,
        selected_adapter,
        device.paired,
        device.trusted,
        device.connected,
    )
    session = attempt.setdefault("session", quirks_report.session_environment())
    if isinstance(session, dict):
        session["bluetooth_owners"] = _bluetooth_session_owners()
    teardown = _take_pending_teardown(selected_adapter)
    if teardown is not None:
        teardown["before_new_pairing"] = _bluez_device_snapshot(device.device_path)
        attempt["previous_teardown"] = teardown
    quirks_report.mark(attempt, "device_loaded", already_paired=device.paired)
    _snapshot_phone(attempt, device)
    _record_bluez_state(attempt, device.device_path, "device_loaded", force=True)

    compatibility = bluetooth_compatibility(selected_adapter)
    attempt["controller"] = _controller_snapshot(selected_adapter, compatibility)
    quirks_report.mark(attempt, "compatibility_ready")
    if not compatibility["hardware_supported"]:
        issue = compatibility["issue"] or "Controller capabilities could not be verified"
        log.warning(
            "controller capability advisory: %s; continuing with pairing",
            issue,
        )
        quirks_report.mark(attempt, "compatibility_advisory", issue=issue)
    policy = resolve_pairing_policy(
        compatibility,
        force_compatibility=compatibility_mode,
        force_explicit_pairing=explicit_pairing,
        interactive=interactive,
    )
    attempt["pairing_policy"] = policy.to_dict()
    log.info(
        "pairing policy: mode=%s strategy=%s ANCS=%s solicitation=%s (%s)",
        policy.mode.value,
        policy.pairing_strategy,
        "enabled" if policy.ancs_enabled else "disabled",
        "enabled" if policy.solicitation_enabled else "unavailable",
        policy.reason,
    )
    if policy.ancs_enabled and not compatibility["bearer_api_active"]:
        raise PairingError(
            "Activate Bluetooth support before pairing or re-pairing the iPhone"
        )
    return _PairingPreparation(
        device=device,
        adapter=selected_adapter,
        policy=policy,
    )


def _prepare_classic_transport(adapter: str, attempt: PairingAttempt) -> None:
    from blueferry import bluez_setup

    # Advertising before the bond exists can make iOS retain two accessories.
    quirks_report.mark(attempt, "prepare_classic_sent")
    if not bluez_setup.prepare_classic(adapter=adapter, authorize=True):
        raise PairingError(
            "Could not prepare the Bluetooth adapter; check that "
            "blueferry-backend is installed and run doctor."
        )
    quirks_report.mark(attempt, "prepare_classic_done")
    _activate_obex_mns()
    quirks_report.mark(attempt, "obex_mns_ready")


def _run_pairing_transaction(
    mac: str,
    device: PairedDevice,
    adapter: str,
    policy: PairingPolicy,
    *,
    confirmation: ConfirmationCallback | None,
    display: DisplayCallback | None,
    attempt: PairingAttempt,
    resources: _PairingResources,
) -> PairedDevice:
    if device.paired:
        return device
    try:
        if confirmation is None:
            # complete_pairing permits this only with explicit _allow_headless.
            log.info("sending Device1.Pair using the headless pairing path")
            quirks_report.mark(attempt, "pair_sent")
            dbus.Interface(
                get_system_bus().get_object("org.bluez", device.device_path),
                "org.bluez.Device1",
            ).Pair(timeout=120.0)
            quirks_report.mark(attempt, "pair_replied")
        else:
            from blueferry.pairing_agent import RegisteredPairingAgent

            def timed_confirmation(passkey: int | None) -> bool:
                quirks_report.mark(
                    attempt,
                    "pairing_confirmation_requested",
                    has_passkey=passkey is not None,
                )
                accepted = confirmation(passkey)
                quirks_report.mark(
                    attempt,
                    "pairing_confirmation_answered",
                    accepted=accepted,
                )
                return accepted

            def timed_display(passkey: int) -> None:
                quirks_report.mark(attempt, "pairing_passkey_displayed")
                if display is not None:
                    display(passkey)

            registered = resources.agents.enter_context(
                RegisteredPairingAgent(
                    device.device_path,
                    timed_confirmation,
                    timed_display,
                    make_default=policy.iphone_initiated,
                )
            )
            resources.agent_registered = True
            quirks_report.mark(attempt, "agent_registered")
            if policy.iphone_initiated:
                _connect_classic(
                    device.device_path,
                    timeout=60.0,
                    settle=False,
                    attempt=attempt,
                )
                registered.wait_for_pair(timeout=120.0)
                attempt["pairing_transaction"] = "iphone-initiated-connect"
            else:
                quirks_report.mark(attempt, "pair_sent")
                registered.pair(timeout=120.0)
                quirks_report.mark(attempt, "pair_replied")
                attempt["pairing_transaction"] = "explicit-device-pair"
    except dbus.exceptions.DBusException as error:
        name = error.get_dbus_name() or ""
        if name in {
            "org.bluez.Error.AlreadyExists",
            "org.bluez.Error.InProgress",
        }:
            quirks_report.mark(attempt, "peer_pairing_in_progress")
        else:
            detail = error.get_dbus_message() or name or str(error)
            if name in {
                "org.bluez.Error.AuthenticationCanceled",
                "org.bluez.Error.AuthenticationFailed",
                "org.bluez.Error.AuthenticationRejected",
            }:
                detail = f"Bluetooth confirmation did not complete: {detail}"
            raise PairingError(detail) from error
    paired = _wait_for_paired_device(mac, adapter=adapter)
    quirks_report.mark(attempt, "paired")
    _snapshot_phone(attempt, paired)
    _record_bluez_state(attempt, paired.device_path, "paired", force=True)
    return paired


def _trust_and_settle(device: PairedDevice, attempt: PairingAttempt) -> None:
    log.debug("setting Device1.Trusted=true for %s", device.device_path)
    trust_device(device.mac, device.adapter_path)
    quirks_report.mark(attempt, "trusted")
    _prefer_bredr(device.device_path)
    quirks_report.mark(attempt, "preferred_bearer_bredr")
    _wait_for_classic_settled(device.device_path, attempt=attempt)
    _record_bluez_state(
        attempt,
        device.device_path,
        "classic_settled",
        force=True,
    )


def _register_solicitation(
    device: PairedDevice,
    adapter: str,
    policy: PairingPolicy,
    attempt: PairingAttempt,
    resources: _PairingResources,
) -> None:
    from blueferry import bluez_setup

    if not policy.solicitation_enabled:
        log.info(
            "the controller cannot advertise ANCS solicitation; "
            "continuing with MAP/PBAP"
        )
        return
    log.debug("registering ANCS solicitation advertisement on %s", adapter)
    quirks_report.mark(attempt, "advert_register_sent")
    if not bluez_setup.register_advert(adapter, settle_for_pairing=True):
        raise PairingError("The ANCS advertisement did not activate")
    resources.advert_registered = True
    quirks_report.mark(attempt, "advert_ready")
    _record_bluez_state(attempt, device.device_path, "advert_ready", force=True)


def _handoff_to_daemon(
    device: PairedDevice,
    adapter: str,
    policy: PairingPolicy,
    attempt: PairingAttempt,
) -> PairingTransports:
    if policy.ancs_enabled:
        write_local_env(device.mac, adapter)
    else:
        write_local_env(device.mac, adapter, False)
    log.debug("saved paired target %s; restarting user service", device.mac)
    quirks_report.mark(attempt, "daemon_restart_sent")
    _restart_user_service()
    quirks_report.mark(attempt, "daemon_restarted")
    _record_bluez_state(
        attempt,
        device.device_path,
        "daemon_restarted",
        force=True,
    )
    transports = _wait_for_daemon_transports(
        attempt=attempt,
        notifications_supported=policy.ancs_enabled,
        device_path=device.device_path,
    )
    log.info(
        "pairing-window result: MAP=%s PBAP=%s ANCS=%s",
        transports.map,
        transports.pbap,
        transports.ancs,
    )
    return transports


def _cleanup_pairing_resources(
    device: PairedDevice,
    adapter: str,
    attempt: PairingAttempt,
    resources: _PairingResources,
) -> None:
    from blueferry import bluez_setup

    try:
        if resources.advert_registered:
            log.debug("removing ANCS solicitation advertisement from %s", adapter)
            bluez_setup.unregister_advert(adapter)
            quirks_report.mark(attempt, "advert_removed")
            _record_bluez_state(
                attempt,
                device.device_path,
                "advert_removed",
                force=True,
            )
    finally:
        resources.agents.close()
        if resources.agent_registered:
            quirks_report.mark(attempt, "agent_released")


def _execute_pairing(
    mac: str,
    *,
    adapter: str | None = None,
    confirmation: ConfirmationCallback | None = None,
    display: DisplayCallback | None = None,
    compatibility_mode: bool = False,
    explicit_pairing: bool = False,
    attempt: PairingAttempt,
) -> PairingOutcome:
    preparation = _prepare_pairing(
        mac,
        adapter=adapter,
        compatibility_mode=compatibility_mode,
        explicit_pairing=explicit_pairing,
        interactive=confirmation is not None,
        attempt=attempt,
    )
    device = preparation.device
    selected_adapter = preparation.adapter
    policy = preparation.policy
    _prepare_classic_transport(selected_adapter, attempt)

    resources = _PairingResources()
    try:
        device = _run_pairing_transaction(
            mac,
            device,
            selected_adapter,
            policy,
            confirmation=confirmation,
            display=display,
            attempt=attempt,
            resources=resources,
        )
        _trust_and_settle(device, attempt)
        _register_solicitation(
            device,
            selected_adapter,
            policy,
            attempt,
            resources,
        )
        transports = _handoff_to_daemon(
            device,
            selected_adapter,
            policy,
            attempt,
        )
    finally:
        _cleanup_pairing_resources(
            device,
            selected_adapter,
            attempt,
            resources,
        )

    device = _device(mac, adapter=selected_adapter)
    _snapshot_phone(attempt, device)
    _record_bluez_state(attempt, device.device_path, "finished", force=True)
    return PairingOutcome(
        device=device,
        config=str(LOCAL_ENV_PATH),
        service="package-enabled and restarted",
        ancs=(
            "disabled"
            if not policy.ancs_enabled
            else "connected"
            if transports.ancs
            else "daemon connecting"
        ),
        ancs_enabled=policy.ancs_enabled,
        transports=transports,
        iphone_steps=(
            "Open Settings → Bluetooth and tap ⓘ next to this computer",
            "If this computer is listed twice, check both entries",
            "Toggle on Show Message Notifications and Sync Contacts",
            "You may need to back out and tap ⓘ again to make these toggles appear",
        ),
    )


def forget_device(mac: str, *, adapter: str | None = None) -> None:
    """Stop BlueFerry, remove the local bond, and clear the selected phone."""
    normalized = mac.strip().upper()
    if not config.is_valid_mac(normalized):
        raise PairingError("invalid Bluetooth device address")
    device = _find_device(normalized, adapter=adapter)
    adapter_name = (
        device.adapter_path.rsplit("/", 1)[-1]
        if device is not None
        else (adapter or config.ADAPTER)
    )
    device_path = (
        device.device_path
        if device is not None
        else f"/org/bluez/{adapter_name}/dev_{normalized.replace(':', '_')}"
    )
    teardown: dict[str, object] = {
        "reason": "forget_device",
        "remove_requested": device is not None,
        "before_remove": _bluez_device_snapshot(device_path),
    }
    started = time.monotonic()
    log.info("stopping the user service before forgetting target %s", normalized)
    _stop_user_service()
    if device is not None:
        try:
            log.info("sending Adapter1.RemoveDevice for %s", device.device_path)
            dbus.Interface(
                get_system_bus().get_object("org.bluez", device.adapter_path),
                "org.bluez.Adapter1",
            ).RemoveDevice(dbus.ObjectPath(device.device_path), timeout=30.0)
            teardown["remove_result"] = "replied"
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() != "org.bluez.Error.DoesNotExist":
                teardown["remove_result"] = "failed"
                teardown["remove_error"] = error.get_dbus_name() or type(error).__name__
                teardown["after_remove_reply"] = _bluez_device_snapshot(device_path)
                teardown["remove_elapsed_s"] = round(time.monotonic() - started, 3)
                _save_pending_teardown(adapter_name, teardown)
                raise PairingError(
                    error.get_dbus_message() or error.get_dbus_name() or str(error)
                ) from error
            teardown["remove_result"] = "already_absent"
    else:
        log.debug("no BlueZ device record remains for %s", normalized)
        teardown["remove_result"] = "not_found"
    teardown["after_remove_reply"] = _bluez_device_snapshot(device_path)
    teardown["remove_elapsed_s"] = round(time.monotonic() - started, 3)
    _save_pending_teardown(adapter_name, teardown)
    log.debug("BlueZ teardown trace: %s", teardown)
    clear_local_target()
    log.info("cleared configured BlueFerry target %s", normalized)


def prepare_target_replacement(
    previous_mac: str, next_mac: str, *, adapter: str | None = None,
) -> None:
    """Clear the saved target without losing the unpaired device being selected."""
    previous = previous_mac.strip().upper()
    selected = next_mac.strip().upper()
    if not config.is_valid_mac(previous) or not config.is_valid_mac(selected):
        raise PairingError("invalid Bluetooth device address")
    device = _find_device(previous, adapter=adapter)
    if previous != selected or (device is not None and device.paired):
        forget_device(previous, adapter=adapter)
        return
    # A stale saved MAC can refer to the unpaired device just discovered for
    # this attempt. Removing that BlueZ object would also remove the scan
    # result before complete_pairing() can address it.
    _stop_user_service()
    adapter_name = (
        device.adapter_path.rsplit("/", 1)[-1]
        if device is not None
        else (adapter or config.ADAPTER)
    )
    device_path = (
        device.device_path
        if device is not None
        else f"/org/bluez/{adapter_name}/dev_{previous.replace(':', '_')}"
    )
    teardown = {
        "reason": "same_unpaired_scan_result_retained",
        "remove_requested": False,
        "before_clear": _bluez_device_snapshot(device_path),
    }
    _save_pending_teardown(adapter_name, teardown)
    log.debug("BlueZ teardown trace: %s", teardown)
    clear_local_target()
    log.info("cleared stale BlueFerry target %s before pairing", previous)


def clear_local_target() -> Path:
    """Forget the selected phone without discarding unrelated preferences."""
    existing = config.read_local_env(LOCAL_ENV_PATH)
    existing.pop("BLUEFERRY_MAC", None)
    existing.pop("BLUEFERRY_ADAPTER", None)
    existing.pop("BLUEFERRY_ANCS_ENABLED", None)
    content = "".join(
        f"{key}={existing[key]}\n" for key in sorted(config.LOCAL_ENV_KEYS) if key in existing
    )
    try:
        atomic_write_private_text(
            LOCAL_ENV_PATH,
            content,
            maximum_bytes=config.MAX_CONFIG_FILE_BYTES,
        )
    except (OSError, ValueError) as error:
        raise PairingError(f"could not clear private configuration: {error}") from error
    try:
        clear_setup_verification()
    except (OSError, ValueError) as error:
        raise PairingError(f"could not clear setup verification: {error}") from error
    return LOCAL_ENV_PATH


def write_local_env(
    mac: str,
    adapter: str | None = None,
    ancs_enabled: bool = True,
) -> Path:
    normalized_mac = mac.strip().upper()
    if not config.is_valid_mac(normalized_mac):
        raise PairingError("invalid Bluetooth device address")
    if adapter is not None and not config.is_valid_adapter(adapter):
        raise PairingError("invalid Bluetooth adapter name")
    existing = config.read_local_env(LOCAL_ENV_PATH)
    existing["BLUEFERRY_MAC"] = normalized_mac
    if adapter:
        existing["BLUEFERRY_ADAPTER"] = adapter
    existing["BLUEFERRY_ANCS_ENABLED"] = "true" if ancs_enabled else "false"
    ordered = ["BLUEFERRY_MAC", "BLUEFERRY_ADAPTER", "BLUEFERRY_ANCS_ENABLED"]
    ordered.extend(sorted(config.LOCAL_ENV_KEYS - set(ordered)))
    content = "".join(f"{key}={existing[key]}\n" for key in ordered if key in existing)
    try:
        atomic_write_private_text(
            LOCAL_ENV_PATH,
            content,
            maximum_bytes=config.MAX_CONFIG_FILE_BYTES,
        )
    except (OSError, ValueError) as error:
        raise PairingError(f"could not save private configuration: {error}") from error
    return LOCAL_ENV_PATH
