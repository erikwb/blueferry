"""Bluetooth controller capability probing and packaged BlueZ activation."""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import dbus

from blueferry.config import is_valid_adapter
from blueferry.errors import CommandError, PairingError


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


RunCommand = Callable[..., CommandResult]

# Bluetooth SIG company identifiers seen on common Linux adapters.
_BT_COMPANIES = {
    2: "Intel",
    10: "Qualcomm",
    15: "Broadcom",
    29: "Qualcomm",
    70: "MediaTek",
    93: "Realtek",
    305: "Cypress",
}

# USB/PCI IDs whose sysfs product string is generic (e.g. Wireless_Device).
_CHIPSETS = {
    "0bda:8771": "RTL8761BU",
    "0bda:8852": "RTL8852AE",
    "0bda:b85b": "RTL8852BE",
    "0bda:b85c": "RTL8852BE",
    "0bda:c852": "RTL8852CE",
    "0bda:c85a": "RTL8852CE",
    "0e8d:7922": "MT7922",
    "0e8d:7961": "MT7921",
    "10ec:8852": "RTL8852AE",
    "10ec:b852": "RTL8852BE",
    "10ec:c852": "RTL8852CE",
    "13d3:3563": "MT7922",
    "13d3:3585": "MT7922",
    "14c3:0608": "MT7921",
    "14c3:0616": "MT7922",
    "14c3:0717": "MT7925",
    "14c3:7922": "MT7922",
    "14c3:7961": "MT7921",
    "8086:2723": "AX200",
    "8086:2725": "AX210",
    "8086:51f0": "AX211",
    "8086:54f0": "AX211",
    "8086:7e40": "AX211",
    "8086:7e70": "AX211",
}
_DRIVER_CHIPSETS = {
    "mt7921e": "MT7921",
    "mt7921u": "MT7921",
    "mt7925e": "MT7925",
    "rtw89_8852ae": "RTL8852AE",
    "rtw89_8852be": "RTL8852BE",
    "rtw89_8852ce": "RTL8852CE",
}
_GENERIC_PRODUCTS = {
    "bluetooth",
    "bluetooth adapter",
    "bluetooth device",
    "bluetooth radio",
    "generic",
    "usb",
    "wireless",
    "wireless device",
}

# USB/PCI vendor IDs. Values are lowercase hex without 0x.
_BUS_VENDORS = {
    "8086": "Intel",
    "8087": "Intel",
    "0bda": "Realtek",
    "10ec": "Realtek",
    "14c3": "MediaTek",
    "0e8d": "MediaTek",
    "0a5c": "Broadcom",
    "14e4": "Broadcom",
    "0cf3": "Qualcomm",
    "168c": "Qualcomm",
    "17cb": "Qualcomm",
    "13d3": "AzureWave",
    "04ca": "Lite-On",
}

_BTMGMT_IDENTITY = re.compile(
    r"\bversion\s+(?P<version>\d+)\s+manufacturer\s+(?P<manufacturer>\d+)\b",
    re.IGNORECASE,
)
_MAC_IN_TEXT = re.compile(r"(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def _parse_btmgmt_info(stdout: str) -> tuple[set[str], set[str], dict[str, int]]:
    supported: set[str] = set()
    current: set[str] = set()
    for raw in stdout.splitlines():
        line = raw.strip().casefold()
        if line.startswith("supported settings:"):
            supported.update(line.partition(":")[2].split())
        elif line.startswith("current settings:"):
            current.update(line.partition(":")[2].split())
    identity: dict[str, int] = {}
    match = _BTMGMT_IDENTITY.search(stdout)
    if match is not None:
        identity["hci_version"] = int(match.group("version"))
        identity["manufacturer_id"] = int(match.group("manufacturer"))
    return supported, current, identity


def controller_settings(
    adapter: str, *, run_command: RunCommand, timeout: float = 15,
) -> tuple[bool, set[str], set[str], str, dict[str, int]]:
    index = adapter.removeprefix("hci")
    try:
        result = run_command(
            ["/usr/bin/btmgmt", "--index", index, "info"], timeout=timeout, check=False,
        )
    except CommandError as error:
        return False, set(), set(), str(error), {}
    supported, current, identity = _parse_btmgmt_info(result.stdout)
    return result.returncode == 0 and bool(supported), supported, current, "", identity


def controller_hardware(
    adapter: str,
    *,
    run_command: RunCommand | None = None,
    sys_root: Path = Path("/sys"),
) -> dict[str, object]:
    """Describe the local controller without using the adapter address."""
    identity: dict[str, object] = {"name": adapter}
    if not is_valid_adapter(adapter):
        return identity
    if run_command is not None:
        _apply_btmgmt_identity(identity, adapter, run_command)
    identity.update(_sysfs_identity(adapter, sys_root=sys_root))
    apply_chipset(identity)
    manufacturer_id = identity.get("manufacturer_id")
    if isinstance(manufacturer_id, int):
        apply_company_id(identity, manufacturer_id)
    else:
        identity["summary"] = _hardware_summary(identity)
    return identity


def apply_company_id(identity: dict[str, object], manufacturer_id: int) -> None:
    """Fill vendor/summary from a Bluetooth SIG company identifier."""
    identity["manufacturer_id"] = manufacturer_id
    company = _BT_COMPANIES.get(manufacturer_id)
    if company and not identity.get("vendor"):
        identity["vendor"] = company
    apply_chipset(identity)
    identity["summary"] = _hardware_summary(identity)


def _normalized_product(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())


def is_generic_product(value: str) -> bool:
    folded = _normalized_product(value)
    return not folded or folded in _GENERIC_PRODUCTS


def chipset_name(
    *,
    usb_id: str = "",
    pci_id: str = "",
    driver: str = "",
    product: str = "",
) -> str:
    """Return a chip name when the USB product string is not useful."""
    for raw in (usb_id, pci_id):
        chip = _CHIPSETS.get(str(raw).casefold())
        if chip:
            return chip
    chip = _DRIVER_CHIPSETS.get(str(driver).casefold())
    if chip:
        return chip
    if product and not is_generic_product(product):
        return product.strip()
    return ""


def apply_chipset(identity: dict[str, object]) -> None:
    """Replace a generic USB product string with a known chipset name."""
    product = str(identity.get("product") or "")
    chip = chipset_name(
        usb_id=str(identity.get("usb_id") or ""),
        pci_id=str(identity.get("pci_id") or ""),
        driver=str(identity.get("driver") or ""),
        product=product,
    )
    if chip and (not product or is_generic_product(product)):
        identity["product"] = chip


def _apply_btmgmt_identity(
    identity: dict[str, object], adapter: str, run_command: RunCommand,
) -> None:
    _available, _supported, _current, _error, parsed = controller_settings(
        adapter, run_command=run_command,
    )
    identity.update(parsed)


def _sysfs_identity(adapter: str, *, sys_root: Path) -> dict[str, object]:
    node = sys_root / "class" / "bluetooth" / adapter / "device"
    if not node.exists():
        return {}
    try:
        resolved = node.resolve()
    except OSError:
        resolved = node
    identity: dict[str, object] = {}
    vendor = _sysfs_hex(resolved / "vendor")
    device = _sysfs_hex(resolved / "device")
    if vendor and device:
        identity["bus"] = "pci"
        identity["pci_id"] = f"{vendor}:{device}"
        name = _BUS_VENDORS.get(vendor)
        if name:
            identity["vendor"] = name
    driver = resolved / "driver"
    if driver.is_symlink() or driver.exists():
        try:
            identity["driver"] = driver.resolve().name
        except OSError:
            pass
    for current in (resolved, *list(resolved.parents)[:6]):
        id_vendor = _sysfs_text(current / "idVendor")
        id_product = _sysfs_text(current / "idProduct")
        if not id_vendor or not id_product:
            continue
        identity["bus"] = "usb"
        identity["usb_id"] = f"{id_vendor}:{id_product}"
        name = _BUS_VENDORS.get(id_vendor.casefold())
        if name:
            identity["vendor"] = name
        product = _safe_model(_sysfs_text(current / "product"))
        manufacturer = _safe_model(_sysfs_text(current / "manufacturer"))
        if product:
            identity["product"] = product
        if manufacturer:
            identity["usb_manufacturer"] = manufacturer
        break
    return identity


def _sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _sysfs_hex(path: Path) -> str:
    raw = _sysfs_text(path).casefold()
    if raw.startswith("0x"):
        raw = raw[2:]
    return raw if re.fullmatch(r"[0-9a-f]{4}", raw) else ""


def _safe_model(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 80 or _MAC_IN_TEXT.search(cleaned):
        return ""
    return cleaned


def _hardware_summary(identity: dict[str, object]) -> str:
    vendor = str(identity.get("vendor") or "")
    product = str(identity.get("product") or "")
    chip = " ".join(part for part in (vendor, product) if part) or str(
        identity.get("name") or "unknown"
    )
    bus_id = str(identity.get("usb_id") or identity.get("pci_id") or "")
    bus = str(identity.get("bus") or "")
    driver = str(identity.get("driver") or "")
    details = []
    if bus and bus_id:
        details.append(f"{bus} {bus_id}")
    elif bus_id:
        details.append(bus_id)
    if driver:
        details.append(driver)
    if details:
        return f"{chip} ({', '.join(details)})"
    return chip


def bluez_support_status(
    *,
    run_command: RunCommand,
    proc_root: Path = Path("/proc"),
) -> dict:
    """Report whether the running daemon has the experimental API enabled."""
    try:
        configured = run_command(
            ["/usr/bin/systemctl", "show", "bluetooth.service", "--property=ExecStart", "--value"],
            timeout=15,
            check=False,
        )
        pid_result = run_command(
            ["/usr/bin/systemctl", "show", "bluetooth.service", "--property=MainPID", "--value"],
            timeout=15,
            check=False,
        )
    except CommandError as error:
        raise PairingError(f"Could not inspect bluetooth.service: {error}") from error
    command = configured.stdout.strip()
    active = False
    pid_text = pid_result.stdout.strip()
    if configured.returncode == 0 and pid_result.returncode == 0 and pid_text.isdigit():
        pid = int(pid_text)
        if pid > 0:
            try:
                argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
            except OSError:
                argv = []
            active = b"-E" in argv or b"--experimental" in argv
    drop_in = Path("/usr/lib/systemd/system/bluetooth.service.d/blueferry.conf")
    return {
        "active": active,
        "packaged_drop_in": drop_in.exists(),
        "exec_start": command,
    }


_BLUEZ_DAEMONS = (
    "/usr/lib/bluetooth/bluetoothd",
    "/usr/libexec/bluetooth/bluetoothd",
    "/usr/sbin/bluetoothd",
)
_BLUEZ_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_MIN_BLUEZ_BEARER_API = (5, 86)


def bluez_bearer_api_supported(version: object) -> bool:
    """Return whether BlueZ has working per-bearer Connect/Disconnect methods."""
    match = _BLUEZ_VERSION.fullmatch(str(version).strip())
    if match is None:
        return False
    parts = tuple(int(part) for part in match.group(1).split("."))
    return (parts + (0, 0))[:2] >= _MIN_BLUEZ_BEARER_API


def bluez_stack(
    *,
    run_command: RunCommand,
    experimental: bool | None = None,
    timeout: float = 10,
    proc_root: Path = Path("/proc"),
) -> dict[str, object]:
    """Return the running BlueZ version and whether ``-E`` is enabled."""
    version = ""
    candidates = [["bluetoothctl", "--version"]]
    candidates.extend([path, "--version"] for path in _BLUEZ_DAEMONS if Path(path).is_file())
    for argv in candidates:
        try:
            result = run_command(argv, timeout=timeout, check=False)
        except CommandError:
            continue
        text = f"{getattr(result, 'stdout', '')} {getattr(result, 'stderr', '')}"
        match = _BLUEZ_VERSION.search(text)
        if match:
            version = match.group(1)
            break
    if experimental is None:
        try:
            experimental = bool(
                bluez_support_status(run_command=run_command, proc_root=proc_root)["active"]
            )
        except PairingError:
            experimental = False
    stack: dict[str, object] = {"experimental": bool(experimental)}
    if version:
        stack["bluez_version"] = version
    return stack


def _hci_sort_key(name: str) -> tuple[int, int | str]:
    suffix = name[3:] if name.startswith("hci") else name
    if suffix.isdigit():
        return (0, int(suffix))
    return (1, name)


def _pairing_capable(inspected: tuple | None) -> bool:
    if inspected is None:
        return False
    available, _supported, _current, _error, _identity, fields = inspected
    return bool(available and fields["classic"] and fields["secure_pairing"])


def adapter_label(name: str, hardware: dict[str, object] | None = None) -> str:
    """Human controller name plus the hci index so two cards stay distinct."""
    hardware = hardware or {}
    vendor = str(hardware.get("vendor") or "").strip()
    product = str(hardware.get("product") or "").strip()
    chip = chipset_name(
        usb_id=str(hardware.get("usb_id") or ""),
        pci_id=str(hardware.get("pci_id") or ""),
        driver=str(hardware.get("driver") or ""),
        product=product,
    )
    pretty = chip or (product if product and not is_generic_product(product) else "")
    if vendor and pretty:
        pretty = pretty if vendor.casefold() in pretty.casefold() else f"{vendor} {pretty}"
    else:
        pretty = pretty or vendor
    if pretty:
        return f"{pretty} ({name})"
    return name


def _profile_fields(
    adapter: str,
    available: bool,
    supported: set[str],
    current: set[str],
    command_error: str,
    bearer_active: bool,
    bearer_supported: bool,
) -> dict[str, object]:
    classic = bool({"br/edr", "bredr"} & supported)
    low_energy = "le" in supported
    advertising = "advertising" in supported
    secure_pairing = bool({"ssp", "secure-conn"} & supported)
    messages_supported = available and classic and secure_pairing
    notifications_supported = (
        available and low_energy and advertising and bearer_supported
    )
    missing = [
        label for present, label in (
            (classic, "BR/EDR"), (secure_pairing, "secure pairing")
        ) if not present
    ]
    if not available:
        issue = command_error or f"Bluetooth adapter {adapter} is unavailable"
    elif missing:
        issue = "Controller lacks " + ", ".join(missing)
    elif notifications_supported and not bearer_active:
        issue = "Bluetooth support must be activated before pairing"
    elif not notifications_supported:
        issue = "Messages and contacts are supported; per-app notifications are not"
    else:
        issue = ""
    return {
        "available": available,
        "powered": "powered" in current,
        "classic": classic,
        "low_energy": low_energy,
        "advertising": advertising,
        "secure_pairing": secure_pairing,
        "secure_conn": "secure-conn" in current,
        "hardware_supported": messages_supported,
        "messages_supported": messages_supported,
        "notifications_supported": notifications_supported,
        "bearer_api_supported": bearer_supported,
        "bearer_api_active": bearer_active,
        # Capability discovery selects a mode; the pairing transaction decides
        # whether the adapter actually works.
        "pairing_ready": True,
        "issue": issue,
        "supported_settings": sorted(supported),
        "current_settings": sorted(current),
    }


def compatibility(
    requested: str,
    *,
    adapter_name: str | None,
    object_manager,
    run_command: RunCommand,
    support_status: Callable[[], dict],
) -> dict:
    """Describe supported profiles from capabilities, never vendor names."""
    try:
        managed = object_manager().GetManagedObjects()
        discovered = [
            str(path).rsplit("/", 1)[-1]
            for path, interfaces in managed.items()
            if "org.bluez.Adapter1" in interfaces
        ]
    except dbus.exceptions.DBusException:
        discovered = []
    names: list[str] = []
    for name in discovered:
        if name and is_valid_adapter(name) and name not in names:
            names.append(name)
    if (
        adapter_name
        and is_valid_adapter(adapter_name)
        and adapter_name not in names
    ):
        names.append(adapter_name)
    if not names and requested and is_valid_adapter(requested):
        names = [requested]
    names.sort(key=_hci_sort_key)
    try:
        support = support_status()
    except PairingError:
        support = {}
    bearer_active = bool(support.get("active"))
    bearer_configurable = bearer_active or bool(support.get("packaged_drop_in"))
    stack = bluez_stack(run_command=run_command, experimental=bearer_active)
    bearer_supported = (
        bluez_bearer_api_supported(stack.get("bluez_version"))
        and bearer_configurable
    )
    options: list[dict[str, object]] = []
    inspected: dict[str, tuple] = {}
    for name in names:
        available, supported, current, error, identity = controller_settings(
            name, run_command=run_command,
        )
        hardware = controller_hardware(name, run_command=None)
        manufacturer_id = identity.get("manufacturer_id")
        if isinstance(manufacturer_id, int):
            apply_company_id(hardware, manufacturer_id)
        else:
            apply_chipset(hardware)
            hardware["summary"] = _hardware_summary(hardware)
        fields = _profile_fields(
            name,
            available,
            supported,
            current,
            error,
            bearer_active and bearer_supported,
            bearer_supported,
        )
        options.append(
            {
                "name": name,
                "label": adapter_label(name, hardware),
                "available": bool(fields["available"]),
                "powered": bool(fields["powered"]),
                "hardware_supported": bool(fields["hardware_supported"]),
                "notifications_supported": bool(fields["notifications_supported"]),
                "pairing_ready": bool(fields["pairing_ready"]),
                "issue": str(fields["issue"]),
            }
        )
        inspected[name] = (available, supported, current, error, identity, fields)
    if adapter_name is not None and adapter_name in inspected:
        chosen = adapter_name
    elif _pairing_capable(inspected.get(requested)):
        chosen = requested
    else:
        chosen = next(
            (
                str(option["name"])
                for option in options
                if _pairing_capable(inspected.get(str(option["name"])))
            ),
            names[0],
        )
    _available, _supported, _current, _error, identity, fields = inspected[str(chosen)]
    result: dict[str, object] = {
        "adapter": chosen,
        **fields,
        **stack,
        "adapters": options,
    }
    if "manufacturer_id" in identity:
        result["manufacturer_id"] = identity["manufacturer_id"]
    if "hci_version" in identity:
        result["hci_version"] = identity["hci_version"]
    return result


def activate_bluez_support(
    *,
    status: Callable[[], dict],
    run_command: RunCommand,
    pkexec_path: Path = Path("/usr/bin/pkexec"),
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Restart Bluetooth through Polkit so the packaged drop-in takes effect."""
    current = status()
    if current["active"]:
        return current
    if not current["packaged_drop_in"]:
        raise PairingError(
            "The blueferry-backend Bluetooth service drop-in is not installed."
        )
    if not pkexec_path.is_file() or not pkexec_path.stat().st_mode & 0o111:
        raise PairingError("Polkit's pkexec command is unavailable")
    try:
        run_command(
            [str(pkexec_path), "/usr/bin/systemctl", "restart", "bluetooth.service"],
            timeout=120,
        )
    except CommandError as error:
        raise PairingError(str(error)) from error
    sleep(2)
    current = status()
    if not current["active"]:
        raise PairingError(
            "Bluetooth restarted, but the experimental bearer API is still inactive"
        )
    return current
