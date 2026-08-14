"""Interactive terminal presentation for the pairing application service."""

from __future__ import annotations

import time

import typer

from blueferry.backend_lifecycle import ensure_backend_current
from blueferry.bluetooth_devices import iphone_candidates
from blueferry.client import BackendClient, BackendError
from blueferry.errors import PairingError
from blueferry.quirks_report import cli_issue_hint
from blueferry.setup_client import DISCOVERY_SECONDS, SetupClient
from blueferry.setup_verification import (
    NOTIFICATION_ACCESS,
    remaining_iphone_setup_tasks,
)
from blueferry.text_safety import terminal_text


def _confirm_pairing(passkey: int | None) -> bool:
    if passkey is None:
        prompt = "Approve this Bluetooth pairing request?"
    else:
        prompt = f"Do both devices show Bluetooth code {passkey:06d}?"
    return typer.confirm(prompt, default=False)


def _display_pairing_code(passkey: int) -> None:
    typer.echo(f"Bluetooth pairing code: {passkey:06d}")


def _verification_detail(*, ancs_ready: bool, compatibility_mode: bool) -> str:
    if ancs_ready:
        return "including iPhone notifications"
    if compatibility_mode:
        return "messages and contacts; ANCS was disabled by compatibility mode"
    return "messages and contacts; this controller has no ANCS support"


def run_wizard(
    *,
    verify_after: bool = True,
    compatibility_mode: bool = False,
    explicit_pairing: bool = False,
) -> int:
    """Run the same full pairing workflow exposed by the graphical clients."""
    typer.echo(
        typer.style("\n=== BlueFerry first-run setup ===\n", fg=typer.colors.CYAN, bold=True)
    )

    setup = SetupClient()
    try:
        configuration = setup.configuration()
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    if configuration.saved:
        typer.echo(
            typer.style(
                f"\nBlueFerry already has {configuration.mac} configured.",
                fg=typer.colors.YELLOW,
            )
        )
        typer.echo("Before answering Yes, forget this PC on the iPhone too:")
        typer.echo("Settings → Bluetooth → (i) next to this PC → Forget This Device")
        if not typer.confirm(
            "Forget BlueFerry's configured target and start fresh?",
            default=False,
        ):
            typer.echo("Existing pairing and configuration left unchanged.")
            return 0
        try:
            setup.forget(
                configuration.mac,
                adapter=getattr(configuration, "adapter", "") or None,
            )
        except PairingError as error:
            typer.echo(typer.style(str(error), fg=typer.colors.RED))
            return 1
        typer.echo(typer.style("✓ Previous target forgotten", fg=typer.colors.GREEN))
        configuration = setup.configuration()

    try:
        compatibility = setup.compatibility()
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    if len(compatibility.adapters) > 1:
        typer.echo("Bluetooth controllers:\n")
        for index, option in enumerate(compatibility.adapters, 1):
            marker = " (selected)" if option.name == compatibility.adapter else ""
            typer.echo(f"  [{index}] {option.label}{marker}")
        raw = typer.prompt("Use which controller?", default="").strip()
        if raw:
            try:
                picked = compatibility.adapters[int(raw) - 1]
            except (ValueError, IndexError):
                typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
                return 1
            try:
                compatibility = setup.compatibility(picked.name)
            except PairingError as error:
                typer.echo(typer.style(str(error), fg=typer.colors.RED))
                return 1
    typer.echo(f"Controller: {compatibility.adapter}")
    if not compatibility.hardware_supported:
        typer.echo(
            typer.style(
                compatibility.issue or "Bluetooth controller is incompatible.",
                fg=typer.colors.RED,
            )
        )
        return 1
    typer.echo(
        typer.style(
            "✓ Controller supports Messages and Contacts over BR/EDR",
            fg=typer.colors.GREEN,
        )
    )
    if not compatibility.notifications_supported:
        typer.echo(
            typer.style(
                "⚠ This controller cannot provide per-app iPhone notifications",
                fg=typer.colors.YELLOW,
            )
        )
    if (
        not compatibility_mode
        and compatibility.notifications_supported
        and not compatibility.bearer_api_active
    ):
        if not typer.confirm(
            "Activate Bluetooth support? This briefly disconnects Bluetooth devices.",
            default=True,
        ):
            return 0
        try:
            setup.activate_bluez()
            compatibility = setup.compatibility(compatibility.adapter)
        except PairingError as error:
            typer.echo(typer.style(str(error), fg=typer.colors.RED))
            return 1

    typer.echo("\nUnlock the iPhone and keep Settings → Bluetooth open while scanning.")
    try:
        devices = setup.devices(
            scan_seconds=DISCOVERY_SECONDS, adapter=compatibility.adapter,
        )
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    if not devices:
        typer.echo(typer.style("No Bluetooth devices found.\n", fg=typer.colors.YELLOW))
        typer.echo("Keep Bluetooth settings open on the iPhone and try again.")
        return 1

    candidates = iphone_candidates(
        devices,
        adapter=compatibility.adapter,
        configured_mac=configuration.mac,
        include_unpaired=True,
    )
    if not candidates:
        typer.echo(
            typer.style(
                f"No iPhone was found on {compatibility.adapter}.",
                fg=typer.colors.YELLOW,
            )
        )
        typer.echo("Keep Bluetooth settings open on the iPhone and try again.")
        return 1
    noun = "iPhone" if len(candidates) == 1 else "iPhones"
    typer.echo(f"Found {len(candidates)} {noun}:\n")
    for index, device in enumerate(candidates, 1):
        trusted = typer.style(
            "trusted" if device.trusted else "untrusted",
            fg=typer.colors.GREEN if device.trusted else typer.colors.YELLOW,
        )
        connected = typer.style(
            "connected" if device.connected else "disconnected",
            fg=typer.colors.GREEN if device.connected else typer.colors.YELLOW,
            dim=not device.connected,
        )
        typer.echo(
            f"  [{index}] {terminal_text(device.name).replace(chr(10), ' ')} "
            f"({device.mac}) "
            f"icon={terminal_text(device.icon).replace(chr(10), ' ')} "
            f"{trusted} {connected}"
        )

    if len(candidates) == 1:
        chosen = candidates[0]
        if not typer.confirm("\nUse this device?", default=True):
            return 0
    else:
        try:
            chosen = candidates[int(typer.prompt("Pick a device by number", default="1")) - 1]
        except (ValueError, IndexError):
            typer.echo(typer.style("Invalid choice.", fg=typer.colors.RED))
            return 1

    typer.echo("\nActivating Bluetooth, then starting secure pairing…")
    if compatibility_mode:
        typer.echo(
            "Compatibility mode: BlueFerry will set up Messages and Contacts "
            "without connecting ANCS."
        )
    if explicit_pairing:
        typer.echo("Explicit pairing: skipping the initial Device1.Connect attempt.")
    try:
        result = setup.complete(
            chosen.mac,
            adapter=compatibility.adapter,
            confirmation=_confirm_pairing,
            display=_display_pairing_code,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
        )
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        if getattr(error, "report_path", None):
            typer.echo(cli_issue_hint())
        return 1
    typer.echo(typer.style("✓ Linux-side setup complete", fg=typer.colors.GREEN))
    if getattr(result, "quirks_report", "") and not result.ancs_ready:
        typer.echo(cli_issue_hint())
    try:
        verified = BackendClient().status().verified_iphone_setup
    except BackendError:
        verified = ()
    ancs_enabled = getattr(
        result,
        "ancs_enabled",
        compatibility.notifications_supported and not compatibility_mode,
    )
    remaining = remaining_iphone_setup_tasks(
        verified,
        notifications_supported=ancs_enabled,
    )
    _print_iphone_steps(
        result.device.uuids,
        remaining=remaining,
        notifications_supported=compatibility.notifications_supported,
        ancs_enabled=ancs_enabled,
        ancs_ready=result.ancs_ready,
    )

    prompt = (
        "Have you completed the remaining iPhone steps? Verify the connection now?"
        if remaining
        else "Verify the connection now?"
    )
    if verify_after and typer.confirm(
        prompt,
        default=True,
    ):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                ensure_backend_current()
                status = BackendClient().status()
            except (BackendError, RuntimeError):
                time.sleep(2)
                continue
            profiles_ready = status.map and status.pbap
            notification_ready = status.ancs or not ancs_enabled
            if profiles_ready and notification_ready:
                detail = _verification_detail(
                    ancs_ready=status.ancs,
                    compatibility_mode=compatibility_mode,
                )
                typer.echo(typer.style(f"✓ Setup verified: {detail}", fg=typer.colors.GREEN))
                typer.echo("New incoming messages will appear automatically.")
                return 0
            time.sleep(2)
        typer.echo(
            typer.style(
                "Profiles are not ready yet. The daemon will keep retrying; "
                "check the two iPhone toggles.",
                fg=typer.colors.YELLOW,
            )
        )
    return 0


def _print_iphone_steps(
    uuids: frozenset[str],
    *,
    remaining: tuple[str, ...],
    notifications_supported: bool,
    ancs_enabled: bool,
    ancs_ready: bool,
) -> None:
    if not remaining:
        return
    typer.echo(typer.style("\n=== On the iPhone ===\n", fg=typer.colors.CYAN, bold=True))
    typer.echo("  1. Open Settings → Bluetooth")
    typer.echo("  2. Tap the (i) next to your computer's name in My Devices")
    typer.echo("  3. Allow Notification Access when prompted")
    typer.echo(
        typer.style(
            "  4. Toggle on Show Message Notifications and Sync Contacts",
            fg=typer.colors.WHITE,
            bold=True,
        )
    )
    typer.echo("     Note: You may need to back out and tap the (i) again to make")
    typer.echo("     these toggles appear.")
    if NOTIFICATION_ACCESS in remaining:
        typer.echo("\nANCS notification access is negotiated during pairing. Some")
        typer.echo("iOS versions do not show a separate system-notification toggle.")
    if NOTIFICATION_ACCESS in remaining or not ancs_enabled:
        typer.echo(
            "Without System Notification access, group texts appear as "
            "individual conversations with their sender."
        )
    typer.echo("If the toggles still do not appear, run `blueferry doctor` before re-pairing.")

    ancs_uuid = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
    if not notifications_supported:
        typer.echo(
            typer.style(
                "\n⚠ This controller supports Messages and Contacts, but not "
                "per-app iPhone notifications.",
                fg=typer.colors.YELLOW,
            )
        )
    elif NOTIFICATION_ACCESS in remaining and not ancs_ready and ancs_uuid not in uuids:
        typer.echo(
            typer.style(
                "\n⚠ ANCS is not visible yet. This is normal while BLE services "
                "settle; do not re-pair based on this alone.",
                fg=typer.colors.YELLOW,
            )
        )
