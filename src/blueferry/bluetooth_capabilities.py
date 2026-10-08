"""Bluetooth controller capability probing and packaged BlueZ activation."""
from __future__ import annotations

import fnmatch
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import dbus
from gi.repository import GLib

from blueferry import service_manager
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

# These vendors usually expose LE + advertising, but iOS ANCS pairing
# does not complete on them. Messages and contacts still work.
ANCS_LIMITED_VENDORS = frozenset({"realtek", "broadcom", "cypress"})

# Adapter-specific client defaults; users can still select Connect-first explicitly.
_EXPLICIT_PAIRING_USB_IDS = frozenset({
    "0bda:8771",  # RTL8761BU: Connect can abort before authentication (#144).
    "0bda:8922",  # Realtek: MAP/PBAP success with explicit pairing (#68).
    "13d3:3586",  # AzureWave RTL8852CE: explicit pairing restored ANCS (#58).
})


def ancs_limited_vendor(vendor: object) -> bool:
    """Return True when iPhone notification pairing is not expected to finish."""
    return str(vendor or "").strip().casefold() in ANCS_LIMITED_VENDORS


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


# Same workaround as the packaged blueferry-set-cod helper (``: | btmgmt``).
BTMGMT_STDIN = ""
BLUEZ_MAIN_CONF = Path("/etc/bluetooth/main.conf")
_CONTROLLER_MODES = frozenset({"dual", "bredr", "le"})


def bluez_controller_mode(path: Path | None = None) -> str:
    """Return the ``[General] ControllerMode`` bluetoothd will use, or ``""``.

    bluetoothd loads main.conf with GKeyFile and compares the value with
    ``strcmp`` (BlueZ ``src/main.c``, ``get_mode``), so this reads the file
    with the same GKeyFile parser: names are case-sensitive, the value keeps
    trailing text and whitespace, and a file GKeyFile rejects makes
    bluetoothd use its defaults. Anything bluetoothd would not recognize
    runs as dual mode and is reported as ``"other"``; ``""`` means unset,
    unreadable or ignored. Only these fixed words are returned, never
    configuration text. The file is world-readable on every distribution
    BlueFerry packages for.
    """
    keyfile = GLib.KeyFile()
    try:
        keyfile.load_from_file(str(path or BLUEZ_MAIN_CONF), GLib.KeyFileFlags.NONE)
        mode = keyfile.get_string("General", "ControllerMode")
    except GLib.Error:
        return ""
    return mode if mode in _CONTROLLER_MODES else "other"


def le_disabled_issue(controller_mode: str = "") -> str:
    """Explain a controller that supports LE but runs with LE switched off.

    BlueFerry cannot fix this by itself. With ``ControllerMode = bredr``,
    bluetoothd registers neither its GATT database nor LEAdvertisingManager1
    for the adapter (BlueZ ``src/adapter.c``, ``adapter_register``), so
    switching LE on in the kernel (``btmgmt le on``) changes nothing until
    bluetoothd restarts in another mode. In the default dual mode bluetoothd
    switches LE back on itself when it starts (``read_info_complete``).
    """
    head = (
        "Bluetooth Low Energy is switched off on this adapter, so iPhone "
        "notifications cannot be set up. "
    )
    if controller_mode == "bredr":
        return head + (
            "BlueZ is configured with ControllerMode = bredr. Set "
            "ControllerMode = dual in /etc/bluetooth/main.conf (or remove the "
            "line), restart bluetoothd, then check again."
        )
    return head + (
        "Restart bluetoothd, which switches LE back on in its default dual "
        "mode, then check again. If LE stays off, make sure "
        "/etc/bluetooth/main.conf does not set ControllerMode = bredr."
    )


def controller_settings(
    adapter: str, *, run_command: RunCommand, timeout: float = 15,
) -> tuple[bool, set[str], set[str], str, dict[str, int]]:
    index = adapter.removeprefix("hci")
    try:
        result = run_command(
            ["/usr/bin/btmgmt", "--index", index, "info"], timeout=timeout, check=False,
            # BlueZ 5.72's btmgmt needs a pollable stdin even for one-shot
            # commands; services and desktop launchers often give /dev/null.
            input_text=BTMGMT_STDIN,
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


# Distribution specifics (Gentoo's BLUETOOTH_OPTS, Alpine's command_args)
# are documented in packaging/openrc/README.md.
OPENRC_BLUEZ_ACTIVATION_HINT = (
    "iPhone notifications need BlueZ experimental mode. Start bluetoothd "
    "with -E (see /etc/conf.d/bluetooth), then run "
    '"sudo rc-service bluetooth restart". This briefly disconnects all '
    "Bluetooth devices."
)
UNMANAGED_BLUEZ_ACTIVATION_HINT = (
    "iPhone notifications need BlueZ experimental mode. Start bluetoothd "
    "with -E and restart the Bluetooth service. This briefly disconnects all "
    "Bluetooth devices."
)


def _experimental_argv(argv: list[bytes]) -> bool:
    # Known limit: bundled short options such as "-nE" are not recognized.
    return b"-E" in argv or b"--experimental" in argv


def _running_bluetoothd_argv(proc_root: Path) -> list[bytes] | None:
    """Return the argv of the running bluetoothd, found by process name.

    Known limit: with /proc mounted ``hidepid=1`` or ``2`` another user's
    bluetoothd is invisible, so experimental mode reads as inactive.
    """
    try:
        entries = sorted(
            (entry for entry in proc_root.iterdir() if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return None
    for entry in entries:
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() != "bluetoothd":
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (OSError, UnicodeError):
            continue
        return [value for value in argv if value]
    return None


def _init_agnostic_support_status(proc_root: Path) -> dict:
    """Inspect the running bluetoothd directly when systemd is not in charge."""
    argv = _running_bluetoothd_argv(proc_root) or []
    active = _experimental_argv(argv)
    status: dict[str, object] = {
        "active": active,
        "packaged_drop_in": False,
        "exec_start": " ".join(value.decode("utf-8", "replace") for value in argv),
    }
    if not active:
        status["activation_hint"] = (
            OPENRC_BLUEZ_ACTIVATION_HINT
            if service_manager.init_system() == service_manager.OPENRC
            else UNMANAGED_BLUEZ_ACTIVATION_HINT
        )
    return status


def bluez_support_status(
    *,
    run_command: RunCommand,
    proc_root: Path = Path("/proc"),
) -> dict:
    """Report whether the running daemon has the experimental API enabled.

    Returns ``active``, ``packaged_drop_in``, and ``exec_start``. Without
    systemd there is no unprivileged activation path, so an inactive result
    also carries ``activation_hint``: the administrator steps to show instead
    of offering an automatic Bluetooth restart.
    """
    if service_manager.init_system() != service_manager.SYSTEMD:
        return _init_agnostic_support_status(proc_root)
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
            active = _experimental_argv(argv)
    drop_in = Path("/usr/lib/systemd/system/bluetooth.service.d/blueferry.conf")
    return {
        "active": active,
        "packaged_drop_in": drop_in.exists(),
        "exec_start": command,
    }


def bluetoothd_argv(proc_root: Path = Path("/proc")) -> list[str] | None:
    """Return the running bluetoothd's argv, found by process name.

    Works without systemd (OpenRC, runit). Returns ``None`` when no
    bluetoothd is visible, e.g. with ``hidepid`` or in a container.
    """
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            if name != "bluetoothd":
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return None


def _plugin_patterns(argv: list[str], short: str, long: str) -> list[str]:
    patterns: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        value: str | None = None
        if token in (short, long):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            index += 1
        elif token.startswith(long + "="):
            value = token[len(long) + 1:]
        elif token.startswith(short) and not token.startswith("--") and len(token) > 2:
            value = token[2:]
        if value is not None:
            patterns.extend(part.strip() for part in value.split(",") if part.strip())
        index += 1
    return patterns


def bluez_hfp_plugin_active(argv: list[str] | None) -> bool:
    """Whether bluetoothd's own HFP hands-free profile is registered.

    BlueZ 5.87's ``profiles/audio/hfp-hf.c`` (plugin ``hfp``) is an
    experimental, auto-connecting hands-free profile: it is live exactly
    when bluetoothd runs with ``-E``, which BlueFerry's bearer API needs, and
    it then competes with oFono for the iPhone's HFP RFCOMM channel.
    ``-P hfp`` (``--noplugin``) or a ``-p`` list without it turns it off.
    Plugin patterns are shell globs, as in bluetoothd. This reads only the
    options; ``bluez_hfp_plugin_possible`` rules out a BlueZ that predates
    the plugin. Callers only use both to explain a bring-up that keeps
    failing.
    """
    if not argv:
        return False
    options = argv[1:]
    if "-E" not in options and "--experimental" not in options:
        return False
    included = _plugin_patterns(options, "-p", "--plugin")
    if included and not any(fnmatch.fnmatchcase("hfp", pattern) for pattern in included):
        return False
    excluded = _plugin_patterns(options, "-P", "--noplugin")
    return not any(fnmatch.fnmatchcase("hfp", pattern) for pattern in excluded)


_BLUEZ_DAEMONS = (
    "/usr/lib/bluetooth/bluetoothd",
    "/usr/libexec/bluetooth/bluetoothd",
    "/usr/sbin/bluetoothd",
)
_BLUEZ_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_MIN_BLUEZ_BEARER_API = (5, 86)
# The bearer interfaces first appeared here, without working methods.
_MIN_BLUEZ_BEARER_INTERFACES = (5, 84)
_MIN_BLUEZ_HFP_PLUGIN = (5, 87)


def _bluez_version_tuple(version: object) -> tuple[int, ...] | None:
    match = _BLUEZ_VERSION.fullmatch(str(version).strip())
    if match is None:
        return None
    parts = tuple(int(part) for part in match.group(1).split("."))
    return (parts + (0, 0))[:2]


def bluez_bearer_api_supported(version: object) -> bool:
    """Return whether BlueZ has working per-bearer Connect/Disconnect methods."""
    parts = _bluez_version_tuple(version)
    return parts is not None and parts >= _MIN_BLUEZ_BEARER_API


def bluez_hfp_plugin_possible(version: object) -> bool:
    """Return whether this BlueZ can contain its own HFP hands-free plugin.

    Only a known older version rules the plugin out; an unknown version does
    not.
    """
    parts = _bluez_version_tuple(version)
    return parts is None or parts >= _MIN_BLUEZ_HFP_PLUGIN


def bluez_lacks_bearer_interfaces(version: object) -> bool:
    """Return whether this BlueZ predates the bearer interfaces altogether.

    Such a daemon behaves the same with or without ``-E``: the backend reads
    the aggregate device state and proves ANCS with a GATT round trip. BlueZ
    5.84 and 5.85 expose bearer state but cannot disconnect a bearer, so they
    are neither.
    """
    parsed = _bluez_version_tuple(version)
    return parsed is not None and parsed < _MIN_BLUEZ_BEARER_INTERFACES


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
    legacy_notifications: bool = False,
    *,
    controller_mode: str = "",
) -> dict[str, object]:
    classic = bool({"br/edr", "bredr"} & supported)
    low_energy = "le" in supported
    # btmgmt reports supported and current settings separately. ``le`` can be
    # supported but switched off, e.g. by ControllerMode = bredr (#192); the
    # ANCS advertisement then never activates.
    le_enabled = "le" in current
    le_disabled = available and low_energy and not le_enabled
    advertising = "advertising" in supported
    secure_pairing = bool({"ssp", "secure-conn"} & supported)
    # MAP/PBAP carry data over Classic, but iOS exposes their permissions only
    # after LE solicitation. Compatibility mode still needs that advertisement.
    messages_supported = available and classic and secure_pairing and low_energy and advertising
    notifications_supported = messages_supported and (
        bearer_supported or legacy_notifications
    )
    # Nothing has to be activated when BlueZ has no bearer API to expose.
    notifications_active = notifications_supported and (
        bearer_active or legacy_notifications
    )
    missing = [
        label for present, label in (
            (classic, "Bluetooth Classic (BR/EDR)"),
            (secure_pairing, "secure pairing"),
            (low_energy, "Bluetooth LE"),
            (advertising, "LE advertising"),
        ) if not present
    ]
    if not available:
        issue = command_error or f"Bluetooth adapter {adapter} is unavailable"
    elif missing:
        issue = (
            "Incompatible Bluetooth adapter: missing " + ", ".join(missing) + ". "
            "BlueFerry requires Bluetooth Classic and Bluetooth 4.0 or newer "
            "with LE advertising to enable iPhone messages and contacts. "
            "Use a compatible adapter."
        )
    elif le_disabled:
        issue = le_disabled_issue(controller_mode)
    elif notifications_supported and not notifications_active:
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
        "le_enabled": le_enabled,
        "le_disabled": le_disabled,
        "advertising": advertising,
        "secure_pairing": secure_pairing,
        "secure_conn": "secure-conn" in current,
        "hardware_supported": messages_supported,
        "messages_supported": messages_supported,
        "notifications_supported": notifications_supported,
        "notifications_active": notifications_active,
        "bearer_api_supported": bearer_supported,
        "bearer_api_active": bearer_active,
        # A failed probe is inconclusive (#28); only confirmed missing
        # capabilities prevent pairing (#143).
        "pairing_ready": not available or messages_supported,
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
    activation_hint = str(support.get("activation_hint") or "")
    stack = bluez_stack(run_command=run_command, experimental=bearer_active)
    bearer_supported = (
        bluez_bearer_api_supported(stack.get("bluez_version"))
        and bearer_configurable
    )
    legacy_notifications = bluez_lacks_bearer_interfaces(stack.get("bluez_version"))
    controller_mode = bluez_controller_mode()
    options: list[dict[str, object]] = []
    inspected: dict[str, tuple] = {}
    hardware_by_name: dict[str, dict[str, object]] = {}
    for name in names:
        available, supported, current, error, identity = controller_settings(
            name, run_command=run_command,
        )
        hardware = controller_hardware(name, run_command=None)
        hardware_by_name[name] = hardware
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
            legacy_notifications,
            controller_mode=controller_mode,
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
    else:
        # Prefer verified hardware, then an inconclusive probe that still
        # permits pairing. Honor the configured adapter within each tier.
        preferred = sorted(options, key=lambda option: option["name"] != requested)
        fallback = next(
            (str(option["name"]) for option in preferred if option["pairing_ready"]),
            names[0],
        )
        chosen = next(
            (str(option["name"]) for option in preferred if option["hardware_supported"]),
            fallback,
        )
    _available, _supported, _current, _error, identity, fields = inspected[str(chosen)]
    hardware = hardware_by_name.get(str(chosen), {})
    vendor = str(hardware.get("vendor") or "")
    result: dict[str, object] = {
        "adapter": chosen,
        **fields,
        **stack,
        "adapters": options,
        "controller_mode": controller_mode,
        "controller_vendor": vendor,
        "ancs_limited_controller": ancs_limited_vendor(vendor),
        "explicit_pairing_default": (
            str(hardware.get("usb_id") or "").casefold() in _EXPLICIT_PAIRING_USB_IDS
        ),
    }
    if activation_hint and bluez_bearer_api_supported(stack.get("bluez_version")):
        # Clients cannot restart bluetoothd here; explain the manual step
        # instead of offering an activation that would always fail.
        result["bluez_activation_hint"] = activation_hint
        if result["messages_supported"] and not result["notifications_supported"]:
            result["issue"] = activation_hint
    if "manufacturer_id" in identity:
        result["manufacturer_id"] = identity["manufacturer_id"]
    if "hci_version" in identity:
        result["hci_version"] = identity["hci_version"]
    return result


def activate_bluez_support(
    *,
    status: Callable[[], dict],
    run_command: RunCommand,
    systemctl_path: Path = Path("/usr/bin/systemctl"),
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Restart Bluetooth through systemd so the packaged drop-in takes effect.

    Other init systems have no unprivileged restart path; their status carries
    the administrator steps instead.
    """
    current = status()
    if current["active"]:
        return current
    if hint := current.get("activation_hint"):
        raise PairingError(str(hint))
    if not current["packaged_drop_in"]:
        raise PairingError(
            "The blueferry-backend Bluetooth service drop-in is not installed."
        )
    if not systemctl_path.is_file() or not systemctl_path.stat().st_mode & 0o111:
        raise PairingError("systemctl is unavailable")
    try:
        run_command(
            [str(systemctl_path), "restart", "bluetooth.service"],
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
