"""Interactive terminal presentation for the pairing application service."""
from __future__ import annotations

import time

import typer

from blueferry.backend_lifecycle import ensure_backend_current
from blueferry.client import BackendClient, BackendError
from blueferry.errors import PairingError
from blueferry.setup_client import SetupClient
from blueferry.text_safety import terminal_text


def run_wizard(*, verify_after: bool = True) -> int:
    """Run the same full pairing workflow exposed by the graphical clients."""
    typer.echo(typer.style("\n=== BlueFerry first-run setup ===\n",
                           fg=typer.colors.CYAN, bold=True))

    setup = SetupClient()
    try:
        compatibility = setup.compatibility()
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    typer.echo(f"Controller: {compatibility.adapter}")
    if not compatibility.hardware_supported:
        typer.echo(typer.style(
            compatibility.issue or "Bluetooth controller is incompatible.",
            fg=typer.colors.RED,
        ))
        return 1
    typer.echo(typer.style(
        "✓ Controller supports Messages and Contacts over BR/EDR",
        fg=typer.colors.GREEN,
    ))
    if not compatibility.notifications_supported:
        typer.echo(typer.style(
            "⚠ This controller cannot provide per-app iPhone notifications",
            fg=typer.colors.YELLOW,
        ))
    if compatibility.notifications_supported and not compatibility.bearer_api_active:
        if not typer.confirm(
            "Activate Bluetooth support? This briefly disconnects Bluetooth devices.",
            default=True,
        ):
            return 0
        try:
            setup.activate_bluez()
        except PairingError as error:
            typer.echo(typer.style(str(error), fg=typer.colors.RED))
            return 1

    typer.echo("\nUnlock the iPhone and keep Settings → Bluetooth open while scanning.")
    try:
        devices = setup.devices(scan_seconds=8)
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    if not devices:
        typer.echo(typer.style("No Bluetooth devices found.\n",
                               fg=typer.colors.YELLOW))
        typer.echo("Keep Bluetooth settings open on the iPhone and try again.")
        return 1

    devices = [
        device for device in devices
        if device.adapter_path.endswith(f"/{compatibility.adapter}")
    ]
    if not devices:
        typer.echo(typer.style(
            f"No devices were found on {compatibility.adapter}.",
            fg=typer.colors.YELLOW,
        ))
        typer.echo("Keep Bluetooth settings open on the iPhone and try again.")
        return 1
    candidates = [device for device in devices if device.likely_iphone] or devices
    noun = "Bluetooth device" if len(candidates) == 1 else "Bluetooth devices"
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

    typer.echo(
        "\nPreparing secure pairing. The matching code can take about "
        "15 seconds to appear."
    )
    try:
        result = setup.complete(chosen.mac)
    except PairingError as error:
        typer.echo(typer.style(str(error), fg=typer.colors.RED))
        return 1
    typer.echo(typer.style("✓ Linux-side setup complete", fg=typer.colors.GREEN))
    _print_iphone_steps(
        result.device.uuids,
        notifications_supported=compatibility.notifications_supported,
        ancs_ready=result.ancs_ready,
    )

    if verify_after and typer.confirm(
        "Have you enabled both iPhone settings? Verify the connection now?",
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
            notification_ready = (
                status.ancs or not compatibility.notifications_supported
            )
            if profiles_ready and notification_ready:
                detail = "including iPhone notifications" if status.ancs else (
                    "messages and contacts; this controller has no ANCS support"
                )
                typer.echo(typer.style(f"✓ Setup verified: {detail}", fg=typer.colors.GREEN))
                typer.echo("New incoming messages will appear automatically.")
                return 0
            time.sleep(2)
        typer.echo(typer.style(
            "Profiles are not ready yet. The daemon will keep retrying; "
            "check the two iPhone toggles.",
            fg=typer.colors.YELLOW,
        ))
    return 0


def _print_iphone_steps(
    uuids: frozenset[str],
    *,
    notifications_supported: bool,
    ancs_ready: bool,
) -> None:
    typer.echo(typer.style("\n=== On the iPhone ===\n",
                           fg=typer.colors.CYAN, bold=True))
    typer.echo("  1. Open Settings → Bluetooth")
    typer.echo("  2. Tap the (i) next to your computer's name in My Devices")
    typer.echo(typer.style("  3. Enable: Show Message Notifications",
                           fg=typer.colors.WHITE, bold=True))
    typer.echo(typer.style("  4. Enable: Sync Contacts",
                           fg=typer.colors.WHITE, bold=True))
    typer.echo("\nANCS notification access is negotiated during pairing. Some")
    typer.echo("iOS versions do not show a separate system-notification toggle.")
    typer.echo("If either toggle is missing, run `blueferry doctor` before re-pairing.")

    ancs_uuid = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
    if not notifications_supported:
        typer.echo(typer.style(
            "\n⚠ This controller supports Messages and Contacts, but not "
            "per-app iPhone notifications.",
            fg=typer.colors.YELLOW,
        ))
    elif not ancs_ready and ancs_uuid not in uuids:
        typer.echo(typer.style(
            "\n⚠ ANCS is not visible yet. This is normal while BLE services "
            "settle; do not re-pair based on this alone.",
            fg=typer.colors.YELLOW,
        ))
