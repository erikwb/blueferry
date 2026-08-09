"""Bluetooth controller capability probing and packaged BlueZ activation."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import dbus

from blueferry.errors import CommandError, PairingError


class CommandResult(Protocol):
    returncode: int
    stdout: str


RunCommand = Callable[..., CommandResult]


def controller_settings(
    adapter: str, *, run_command: RunCommand,
) -> tuple[bool, set[str], set[str], str]:
    index = adapter.removeprefix("hci")
    try:
        result = run_command(
            ["/usr/bin/btmgmt", "--index", index, "info"], timeout=15, check=False,
        )
    except CommandError as error:
        return False, set(), set(), str(error)
    supported: set[str] = set()
    current: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip().casefold()
        if line.startswith("supported settings:"):
            supported.update(line.partition(":")[2].split())
        elif line.startswith("current settings:"):
            current.update(line.partition(":")[2].split())
    return result.returncode == 0 and bool(supported), supported, current, ""


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
        _name, available, settings, _current, _error = inspected
        classic = bool({"br/edr", "bredr"} & settings)
        secure = bool({"ssp", "secure-conn"} & settings)
        if available and classic and secure:
            selected = inspected
            break
    adapter, available, supported, current, command_error = (
        selected or fallback or (requested, False, set(), set(), "No adapter found")
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
    return {
        "adapter": adapter,
        "available": available,
        "powered": "powered" in current,
        "classic": classic,
        "low_energy": low_energy,
        "advertising": advertising,
        "secure_pairing": secure_pairing,
        "hardware_supported": messages_supported,
        "messages_supported": messages_supported,
        "notifications_supported": notifications_supported,
        "bearer_api_active": bearer_active,
        "pairing_ready": messages_supported and (
            bearer_active or not notifications_supported
        ),
        "issue": issue,
        "supported_settings": sorted(supported),
    }


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
