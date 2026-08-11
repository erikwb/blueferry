"""Bluetooth discovery, pairing, and first-run configuration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry import bluetooth_capabilities as capabilities
from blueferry import config
from blueferry.bluetooth_devices import PairedDevice
from blueferry.bus import get_system_bus
from blueferry.commands import run_command
from blueferry.errors import CommandError, PairingError
from blueferry.private_files import atomic_write_private_text
from blueferry.setup_verification import clear_setup_verification

log = logging.getLogger(__name__)

LOCAL_ENV_PATH = config.LOCAL_ENV_PATH
CLASSIC_SETTLE_SECONDS = 3.0

ConfirmationCallback = Callable[[int | None], bool]
DisplayCallback = Callable[[int], None]


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


def bluez_support_status() -> dict:
    """Report whether the packaged experimental bearer API is active."""
    return capabilities.bluez_support_status(run_command=run_command)


def activate_bluez_support() -> dict:
    """Restart Bluetooth via Polkit so the packaged drop-in takes effect."""
    return capabilities.activate_bluez_support(
        status=bluez_support_status,
        run_command=run_command,
    )


def _object_manager():
    return dbus.Interface(
        get_system_bus().get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )


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


def discover_devices(seconds: int = 8) -> list[PairedDevice]:
    """Scan through BlueZ and return paired and newly discovered devices."""
    seconds = max(1, min(int(seconds), 30))
    adapter_path = f"/org/bluez/{config.ADAPTER}"
    discovered: list[PairedDevice] = []
    try:
        objects = _object_manager().GetManagedObjects()
        adapters = [str(path) for path, ifaces in objects.items() if "org.bluez.Adapter1" in ifaces]
        if adapters:
            adapter_path = next(
                (path for path in adapters if path.endswith(f"/{config.ADAPTER}")),
                adapters[0],
            )
        obj = get_system_bus().get_object("org.bluez", adapter_path)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        adapter = dbus.Interface(obj, "org.bluez.Adapter1")
        props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        try:
            adapter.StartDiscovery()
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() != "org.bluez.Error.InProgress":
                raise
        deadline = time.monotonic() + seconds
        while True:
            discovered = list_devices()
            if any(device.likely_iphone for device in discovered):
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
        try:
            adapter.StopDiscovery()
        except (UnboundLocalError, dbus.exceptions.DBusException):
            pass
    return discovered or list_devices()


def trust_device(mac: str, adapter_path: str) -> None:
    dev_path = f"{adapter_path}/dev_{mac.replace(':', '_')}"
    props = dbus.Interface(
        get_system_bus().get_object("org.bluez", dev_path),
        "org.freedesktop.DBus.Properties",
    )
    props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))


def _find_device(mac: str) -> PairedDevice | None:
    normalized = mac.strip().upper()
    for device in list_devices():
        if device.mac.upper() == normalized:
            return device
    return None


def _device(mac: str) -> PairedDevice:
    normalized = mac.strip().upper()
    device = _find_device(normalized)
    if device is not None:
        return device
    raise PairingError(f"Bluetooth device {normalized} is no longer available; scan again")


def _wait_for_paired_device(mac: str, *, timeout: float = 120) -> PairedDevice:
    """Wait for a local or iPhone-initiated pairing transaction to settle."""
    log.debug("waiting up to %.0fs for the %s pairing record to settle", timeout, mac)
    deadline = time.monotonic() + timeout
    device = _device(mac)
    while not device.paired and time.monotonic() < deadline:
        time.sleep(0.25)
        device = _device(mac)
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
            if connected_since is None:
                connected_since = now
            if now - connected_since >= settle_seconds:
                log.info("Classic connection is settled")
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
) -> None:
    """Connect the Classic bearer without blocking agent dispatch."""
    log.info("sending Device1.Connect for Classic bearer: %s", device_path)
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
        raise PairingError(
            error.get_dbus_message() or error.get_dbus_name() or str(error)
        ) from error
    if error is None:
        log.debug("Device1.Connect completed successfully")
    else:
        log.debug("Device1.Connect reports %s", error.get_dbus_name())
    if settle:
        _wait_for_classic_settled(device_path, timeout=timeout)


def _connect_ancs(device: PairedDevice, *, timeout: float = 30.0) -> str:
    """Connect the bonded LE bearer exposed by bluetoothd's experimental API."""
    manager = _object_manager()
    previous_state: tuple[bool, bool, bool] | None = None

    def bearer_present() -> bool:
        nonlocal previous_state
        objects = manager.GetManagedObjects()
        interfaces = objects.get(dbus.ObjectPath(device.device_path), {})
        bearer = interfaces.get("org.bluez.Bearer.LE1")
        if bearer is None:
            return False
        state = (
            bool(bearer.get("Paired", False)),
            bool(bearer.get("Bonded", False)),
            bool(bearer.get("Connected", False)),
        )
        if state != previous_state:
            log.debug(
                "LE bearer probe: paired=%s bonded=%s connected=%s",
                *state,
            )
            previous_state = state
        return True

    log.debug("probing for Bearer.LE1 on %s", device.device_path)
    if not _dispatching_wait(time.monotonic() + timeout, bearer_present):
        raise PairingError(
            "The iPhone has not established the low-energy half of this "
            "bond yet; notification setup continues in the background."
        )

    obj = get_system_bus().get_object("org.bluez", device.device_path)
    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
    try:
        log.debug("setting Device1.PreferredBearer=le")
        props.Set("org.bluez.Device1", "PreferredBearer", dbus.String("le"))
    except dbus.exceptions.DBusException as error:
        name = error.get_dbus_name() or ""
        if name in {
            "org.freedesktop.DBus.Error.UnknownInterface",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.freedesktop.DBus.Error.UnknownProperty",
        }:
            return "waiting for iPhone to connect"
        raise PairingError(
            error.get_dbus_message() or name or str(error)
        ) from error
    for attempt in range(2):
        try:
            log.info("sending Bearer.LE1.Connect (attempt %d/2)", attempt + 1)
            dbus.Interface(obj, "org.bluez.Bearer.LE1").Connect(timeout=45.0)
            log.info("LE bearer connected")
            return "connected"
        except dbus.exceptions.DBusException as error:
            name = error.get_dbus_name() or ""
            detail = error.get_dbus_message() or ""
            if name in {
                "org.bluez.Error.AlreadyConnected",
                "org.bluez.Error.InProgress",
            } or detail.casefold() == "operation already in progress":
                log.info(
                    "LE bearer connection is already active (%s)",
                    name or detail,
                )
                return "already connected"
            if name in {
                "org.freedesktop.DBus.Error.UnknownInterface",
                "org.freedesktop.DBus.Error.UnknownMethod",
                "org.freedesktop.DBus.Error.UnknownProperty",
            }:
                # Marker-only bearer API: the advert invites the bonded iPhone
                # to establish LE inbound instead.
                log.debug("Bearer.LE1.Connect is unavailable: %s", name)
                return "waiting for iPhone to connect"
            log.debug(
                "Bearer.LE1.Connect failed: %s: %s",
                name or "unknown error",
                detail or "(no detail)",
            )
            failure = f"{name} {detail}".casefold()
            if attempt == 0 and "le-connection-abort-by-local" in failure:
                # Desktop managers often connect the newly paired Classic
                # device themselves. Let that link settle again before one
                # bounded retry instead of racing their post-pair work.
                _wait_for_classic_settled(device.device_path, timeout=15.0)
                continue
            raise PairingError(detail or name or str(error)) from error
    raise PairingError("LE connection did not complete")


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
    confirmation: ConfirmationCallback | None = None,
    display: DisplayCallback | None = None,
) -> dict:
    """Pair if needed and finish every Linux-side BlueFerry setup step."""
    from blueferry import bluez_setup

    device = _device(mac)
    log.info(
        "starting setup for %s (%s): paired=%s trusted=%s connected=%s",
        device.name,
        device.mac,
        device.paired,
        device.trusted,
        device.connected,
    )
    adapter = device.adapter_path.rsplit("/", 1)[-1]
    compatibility = bluetooth_compatibility(adapter)
    if not compatibility["hardware_supported"]:
        raise PairingError(compatibility["issue"] or "Bluetooth controller is incompatible")
    notifications_supported = compatibility["notifications_supported"]
    if not notifications_supported:
        raise PairingError(
            "BlueFerry requires a successful ANCS/LE connection before it can "
            "save an iPhone for MAP"
        )
    if notifications_supported and not compatibility["bearer_api_active"]:
        raise PairingError("Activate Bluetooth support before pairing or re-pairing the iPhone")
    # No LE advertisement during pairing: iOS would connect the unbonded
    # advert as a separate accessory and keep two device records. The advert
    # is registered only after the bond exists.
    if not bluez_setup.prepare_classic(adapter=adapter, authorize=True):
        raise PairingError(
            "Could not prepare the Bluetooth adapter; check that "
            "blueferry-backend is installed and run doctor."
        )

    pairing_agents = ExitStack()
    try:
        if not device.paired:
            try:
                if confirmation is None:
                    # Headless fallback: a Linux-initiated transaction pairs
                    # the Classic bearer. Controllers whose Classic pairing
                    # derives LE keys (CTKD) get the dual bond this way too.
                    log.info("sending Device1.Pair using the headless pairing path")
                    dbus.Interface(
                        get_system_bus().get_object("org.bluez", device.device_path),
                        "org.bluez.Device1",
                    ).Pair(timeout=120.0)
                else:
                    from blueferry.pairing_agent import RegisteredPairingAgent

                    # Connecting the unpaired ACL makes iOS initiate pairing,
                    # and the authentication initiator is the side that derives
                    # the cross-transport LE keys. The client has explicit
                    # confirmation UI, so its device-scoped agent temporarily
                    # becomes BlueZ default for this transaction even when a
                    # desktop agent is already registered.
                    registered = pairing_agents.enter_context(
                        RegisteredPairingAgent(
                            device.device_path,
                            confirmation,
                            display,
                        )
                    )
                    _connect_classic(
                        device.device_path,
                        timeout=60.0,
                        settle=False,
                    )
                    registered.wait_for_pair(timeout=120.0)
            except dbus.exceptions.DBusException as error:
                name = error.get_dbus_name() or ""
                if name in {
                    "org.bluez.Error.AlreadyExists",
                    "org.bluez.Error.InProgress",
                }:
                    # The iPhone initiated pairing on its own; settle below.
                    pass
                else:
                    detail = error.get_dbus_message() or name or str(error)
                    if name in {
                        "org.bluez.Error.AuthenticationCanceled",
                        "org.bluez.Error.AuthenticationFailed",
                        "org.bluez.Error.AuthenticationRejected",
                    }:
                        detail = f"Bluetooth confirmation did not complete: {detail}"
                    raise PairingError(detail) from error
            device = _wait_for_paired_device(mac)
        log.debug("setting Device1.Trusted=true for %s", device.device_path)
        trust_device(device.mac, device.adapter_path)

        _connect_classic(device.device_path)
        # Two-phase LE setup, matching the proven desktop sequence: only
        # after the Classic bearer is connected does the ANCS solicitation
        # advert appear, inviting the bonded iPhone to establish LE.
        log.debug("registering ANCS solicitation advertisement on %s", adapter)
        if not bluez_setup.register_advert(adapter, settle_for_pairing=True):
            raise PairingError("The ANCS advertisement did not activate")
        try:
            ancs = _connect_ancs(_device(mac))
        except PairingError as error:
            raise PairingError(
                f"ANCS/LE connection did not complete; the iPhone was not saved: {error}"
            ) from error
        ancs_ready = ancs in {"connected", "already connected"}
        if not ancs_ready:
            raise PairingError("ANCS/LE connection did not complete; the iPhone was not saved")
    finally:
        # The temporary setup process owns this advertisement. Remove it
        # before releasing our device-scoped default agent. Restoring a
        # desktop agent as soon as Device1.Paired flips can let its post-pair
        # work race BlueFerry's Classic-to-LE handoff.
        try:
            log.debug("removing ANCS solicitation advertisement from %s", adapter)
            bluez_setup.unregister_advert(adapter)
        finally:
            pairing_agents.close()

    # The selected MAC is a capability-bearing target, not merely discovery
    # data. Persist it only after ANCS proves that the LE half of this exact
    # bond works; until then the daemon must have no MAP target to connect to.
    write_local_env(device.mac, device.adapter_path.rsplit("/", 1)[-1])
    log.debug("saved paired target %s; restarting user service", device.mac)
    _restart_user_service()
    device = _device(mac)
    return {
        "ok": True,
        "device": device.to_dict(),
        "config": str(LOCAL_ENV_PATH),
        "service": "package-enabled and restarted",
        "ancs": ancs,
        # Reaching this result means ANCS/LE was established before the target
        # was persisted and the daemon was started.
        "ancs_ready": ancs_ready,
        "iphone_steps": [
            "Open Settings → Bluetooth and tap ⓘ next to this computer",
            "If this computer is listed twice, check both entries",
            "Toggle on Show Message Notifications and Sync Contacts",
            "You may need to back out and tap ⓘ again to make these toggles appear",
        ],
    }


def forget_device(mac: str) -> None:
    """Stop BlueFerry, remove the local bond, and clear the selected phone."""
    normalized = mac.strip().upper()
    if not config.is_valid_mac(normalized):
        raise PairingError("invalid Bluetooth device address")
    device = _find_device(normalized)
    log.info("stopping the user service before forgetting target %s", normalized)
    _stop_user_service()
    if device is not None:
        try:
            log.info("sending Adapter1.RemoveDevice for %s", device.device_path)
            dbus.Interface(
                get_system_bus().get_object("org.bluez", device.adapter_path),
                "org.bluez.Adapter1",
            ).RemoveDevice(dbus.ObjectPath(device.device_path), timeout=30.0)
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() != "org.bluez.Error.DoesNotExist":
                raise PairingError(
                    error.get_dbus_message() or error.get_dbus_name() or str(error)
                ) from error
    else:
        log.debug("no BlueZ device record remains for %s", normalized)
    clear_local_target()
    log.info("cleared configured BlueFerry target %s", normalized)


def clear_local_target() -> Path:
    """Forget the selected phone without discarding unrelated preferences."""
    existing = config.read_local_env(LOCAL_ENV_PATH)
    existing.pop("BLUEFERRY_MAC", None)
    existing.pop("BLUEFERRY_ADAPTER", None)
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


def write_local_env(mac: str, adapter: str | None = None) -> Path:
    normalized_mac = mac.strip().upper()
    if not config.is_valid_mac(normalized_mac):
        raise PairingError("invalid Bluetooth device address")
    if adapter is not None and not config.is_valid_adapter(adapter):
        raise PairingError("invalid Bluetooth adapter name")
    existing = config.read_local_env(LOCAL_ENV_PATH)
    existing["BLUEFERRY_MAC"] = normalized_mac
    if adapter:
        existing["BLUEFERRY_ADAPTER"] = adapter
    ordered = ["BLUEFERRY_MAC", "BLUEFERRY_ADAPTER"]
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
