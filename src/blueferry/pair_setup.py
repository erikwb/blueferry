"""Bluetooth discovery, pairing, and first-run configuration."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import dbus
import dbus.exceptions

from blueferry import bluetooth_capabilities as capabilities
from blueferry import config
from blueferry.bluetooth_devices import PairedDevice
from blueferry.bus import get_system_bus
from blueferry.commands import run_command
from blueferry.errors import CommandError, PairingError
from blueferry.private_files import atomic_write_private_text
from blueferry.setup_verification import clear_setup_verification

LOCAL_ENV_PATH = config.LOCAL_ENV_PATH

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
        time.sleep(seconds)
    except dbus.exceptions.DBusException as error:
        raise PairingError(
            error.get_dbus_message() or error.get_dbus_name() or str(error)
        ) from error
    finally:
        try:
            adapter.StopDiscovery()
        except (UnboundLocalError, dbus.exceptions.DBusException):
            pass
    return list_devices()


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
    return device


def _connect_ancs(device: PairedDevice) -> str:
    """Select and connect the bonded LE bearer exposed by bluetoothd -E."""
    obj = get_system_bus().get_object("org.bluez", device.device_path)
    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
    try:
        props.Set("org.bluez.Device1", "PreferredBearer", dbus.String("le"))
        dbus.Interface(obj, "org.bluez.Bearer.LE1").Connect(timeout=45.0)
    except dbus.exceptions.DBusException as error:
        if error.get_dbus_name() in {
            "org.bluez.Error.AlreadyConnected",
            "org.bluez.Error.InProgress",
        }:
            return "already connected"
        name = error.get_dbus_name()
        if name in {
            "org.freedesktop.DBus.Error.UnknownInterface",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.freedesktop.DBus.Error.UnknownProperty",
        }:
            raise PairingError(
                "BlueZ's experimental bearer API is not active. Restart "
                "bluetooth.service once after installing blueferry-backend."
            ) from error
        raise PairingError(error.get_dbus_message() or name or str(error)) from error
    return "connected"


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
    adapter = device.adapter_path.rsplit("/", 1)[-1]
    compatibility = bluetooth_compatibility(adapter)
    if not compatibility["hardware_supported"]:
        raise PairingError(compatibility["issue"] or "Bluetooth controller is incompatible")
    notifications_supported = compatibility["notifications_supported"]
    if notifications_supported and not compatibility["bearer_api_active"]:
        raise PairingError("Activate Bluetooth support before pairing or re-pairing the iPhone")
    if notifications_supported:
        prepared = bluez_setup.prepare(adapter=adapter, authorize=True)
    else:
        cod = bluez_setup.current_cod(adapter)
        prepared = bluez_setup.desired_cod_matches(cod) or bluez_setup.set_cod(
            adapter=adapter, authorize=True
        )
    if not prepared:
        bluez_setup.unregister_advert(adapter)
        raise PairingError(
            "Could not prepare the Bluetooth adapter; check that "
            "blueferry-backend is installed and run doctor."
        )

    try:
        if not device.paired:
            def invoke_pair() -> None:
                device_interface = dbus.Interface(
                    get_system_bus().get_object("org.bluez", device.device_path),
                    "org.bluez.Device1",
                )
                try:
                    device_interface.Pair(timeout=120.0)
                except dbus.exceptions.DBusException as error:
                    if (
                        confirmation is None
                        or error.get_dbus_name() != "org.bluez.Error.InProgress"
                    ):
                        raise
                    # Discovery can provoke an iPhone-initiated transaction,
                    # which would use an unrelated desktop agent. Replace it
                    # with the transaction owned by this registered agent.
                    device_interface.CancelPairing(timeout=10.0)
                    time.sleep(0.25)
                    device_interface.Pair(timeout=120.0)

            try:
                if confirmation is None:
                    invoke_pair()
                else:
                    from blueferry.pairing_agent import RegisteredPairingAgent

                    with RegisteredPairingAgent(
                        device.device_path,
                        confirmation,
                        display,
                    ):
                        invoke_pair()
            except dbus.exceptions.DBusException as error:
                name = error.get_dbus_name() or ""
                if name in {
                    "org.bluez.Error.AlreadyExists",
                    "org.bluez.Error.InProgress",
                }:
                    # The iPhone can initiate pairing while a scan is still
                    # running. Do not start a competing transaction.
                    device = _wait_for_paired_device(mac)
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
        trust_device(device.mac, device.adapter_path)
        write_local_env(device.mac, device.adapter_path.rsplit("/", 1)[-1])
    finally:
        # The temporary setup process owns this advertisement. Remove it
        # before starting the daemon so ownership transfers without a gap or
        # an AlreadyExists false-positive.
        bluez_setup.unregister_advert(adapter)

    _restart_user_service()
    # Give the freshly restarted daemon a moment to claim its bus name and
    # register the long-lived advertisement before selecting LE.
    time.sleep(1)
    device = _device(mac)
    ancs_ready = False
    if notifications_supported:
        try:
            ancs = _connect_ancs(device)
            ancs_ready = True
        except PairingError as error:
            # MAP/PBAP configuration and the bond are already complete. LE
            # service enumeration can lag behind the iPhone permission sheet;
            # that is a pending optional capability, not a failed pair.
            ancs = f"pending: {error}"
    else:
        ancs = "unsupported by Bluetooth controller"
    return {
        "ok": True,
        "device": device.to_dict(),
        "config": str(LOCAL_ENV_PATH),
        "service": "package-enabled and restarted",
        "ancs": ancs,
        "ancs_ready": ancs_ready,
        "iphone_steps": [
            "Open Settings → Bluetooth and tap ⓘ next to this computer",
            "Enable Show Message Notifications",
            "Enable Sync Contacts",
        ],
    }


def forget_device(mac: str) -> None:
    """Stop BlueFerry, remove the local bond, and clear the selected phone."""
    normalized = mac.strip().upper()
    if not config.is_valid_mac(normalized):
        raise PairingError("invalid Bluetooth device address")
    device = _find_device(normalized)
    _stop_user_service()
    if device is not None:
        try:
            dbus.Interface(
                get_system_bus().get_object("org.bluez", device.adapter_path),
                "org.bluez.Adapter1",
            ).RemoveDevice(dbus.ObjectPath(device.device_path), timeout=30.0)
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() != "org.bluez.Error.DoesNotExist":
                raise PairingError(
                    error.get_dbus_message() or error.get_dbus_name() or str(error)
                ) from error
    clear_local_target()


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
