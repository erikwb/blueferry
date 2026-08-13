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


def bluez_support_status(*, run_command: RunCommand) -> dict:
    """Report whether the packaged experimental bearer API is active."""
    try:
        result = run_command(
            ["/usr/bin/systemctl", "show", "bluetooth.service", "--property=ExecStart", "--value"],
            timeout=15,
            check=False,
        )
    except CommandError as error:
        raise PairingError(f"Could not inspect bluetooth.service: {error}") from error
    command = result.stdout.strip()
    active = result.returncode == 0 and (
        " --experimental" in f" {command} " or " -E" in f" {command} "
    )
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


def bluez_stack(
    *,
    run_command: RunCommand,
    experimental: bool | None = None,
    timeout: float = 10,
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
        text = f"{result.stdout} {result.stderr}"
        match = _BLUEZ_VERSION.search(text)
        if match:
            version = match.group(1)
            break
    if experimental is None:
        try:
            experimental = bool(bluez_support_status(run_command=run_command)["active"])
        except PairingError:
            experimental = False
    stack: dict[str, object] = {"experimental": bool(experimental)}
    if version:
        stack["bluez_version"] = version
    return stack


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
        adapters = [
            str(path).rsplit("/", 1)[-1]
            for path, interfaces in managed.items()
            if "org.bluez.Adapter1" in interfaces
        ]
    except dbus.exceptions.DBusException:
        adapters = []
    candidates = (
        [adapter_name]
        if adapter_name is not None
        else list(dict.fromkeys([requested, *adapters]))
    )
    selected = None
    fallback = None
    for candidate in candidates:
        inspected = (
            candidate,
            *controller_settings(candidate, run_command=run_command),
        )
        if fallback is None:
            fallback = inspected
        _name, available, settings, _current, _error, _identity = inspected
        classic = bool({"br/edr", "bredr"} & settings)
        secure = bool({"ssp", "secure-conn"} & settings)
        if available and classic and secure:
            selected = inspected
            break
    adapter, available, supported, current, command_error, identity = (
        selected
        or fallback
        or (requested, False, set(), set(), "No adapter found", {})
    )
    classic = bool({"br/edr", "bredr"} & supported)
    low_energy = "le" in supported
    advertising = "advertising" in supported
    secure_pairing = bool({"ssp", "secure-conn"} & supported)
    messages_supported = available and classic and secure_pairing
    notifications_supported = available and low_energy and advertising
    try:
        bearer_active = bool(support_status()["active"])
    except PairingError:
        bearer_active = False

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
    result: dict[str, object] = {
        "adapter": adapter,
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
        "bearer_api_active": bearer_active,
        "pairing_ready": messages_supported and (
            bearer_active or not notifications_supported
        ),
        "issue": issue,
        "supported_settings": sorted(supported),
        "current_settings": sorted(current),
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
