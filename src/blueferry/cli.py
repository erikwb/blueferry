"""Typer CLI entrypoints."""

from __future__ import annotations

import logging
import os
import shutil

import typer

from blueferry import bluez_setup, config
from blueferry.cli_common import setup_logging as _setup_logging
from blueferry.cli_messages import sms_list, sms_send

app = typer.Typer(
    add_completion=False,
    help="iPhone ↔ Linux Bluetooth bridge using BlueZ.",
)

_OBEXD_PATHS = (
    "/usr/lib/bluetooth/obexd",  # Arch Linux
    "/usr/libexec/bluetooth/obexd",  # Debian/Ubuntu, Fedora
)


def _find_obexd() -> str | None:
    """Find BlueZ's user-session OBEX daemon across common layouts."""
    on_path = shutil.which("obexd")
    if on_path:
        return on_path
    return next((path for path in _OBEXD_PATHS if os.path.isfile(path)), None)


@app.command()
def run(verbose: bool = typer.Option(False, "-v", "--verbose", "--debug")):
    """Start the BlueFerry daemon (runs until Ctrl+C / SIGTERM)."""
    _setup_logging(verbose)
    # Import inside command to avoid loading dbus stack just to print --help
    from blueferry.daemon import Daemon

    exit_code = Daemon().run()
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command()
def doctor(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Check that all prerequisites are in place."""
    _setup_logging(verbose)
    log = logging.getLogger("doctor")

    ok = True

    # BLUEFERRY_MAC configured?
    if config.IPHONE_MAC.upper() in ("AA:BB:CC:DD:EE:FF", ""):
        log.error("BLUEFERRY_MAC not configured (still the placeholder).")
        log.error("    Set your iPhone's Bluetooth MAC via env var, e.g.:")
        log.error("    export BLUEFERRY_MAC=AA:BB:CC:DD:EE:FF")
        log.error("    Or persist it in ~/.config/blueferry/local.env")
        log.error("    (see README — 'Setup'). BlueFerry reads it directly.")
        ok = False
    else:
        log.info("Target MAC configured: %s", config.IPHONE_MAC)

    # BlueZ OBEX daemon present? Distros use different package names and paths.
    obexd = _find_obexd()
    if obexd is None:
        log.error("BlueZ OBEX daemon not found")
        log.error("    Arch: sudo pacman -S bluez-obex")
        log.error("    Debian/Ubuntu: sudo apt install bluez-obexd")
        ok = False
    else:
        log.info("BlueZ OBEX daemon installed: %s", obexd)

    # Adapter CoD
    cod = bluez_setup.current_cod()
    if cod is None:
        log.error("Adapter %s not reachable via DBus", config.ADAPTER)
        ok = False
    else:
        match = bluez_setup.desired_cod_matches(cod)
        if match:
            log.info("Adapter CoD = 0x%06x (A/V Hands-Free)  OK", cod)
        else:
            log.warning(
                "Adapter CoD = 0x%06x — not A/V Hands-Free. "
                "Complete pairing in one of the graphical clients.",
                cod,
            )
            ok = False

    # State dir writable
    try:
        config.ensure_dirs()
        log.info("State dir writable: %s", config.STATE_DIR)
    except OSError as e:
        log.error("State dir not writable: %s", e)
        ok = False

    if ok:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))
    else:
        typer.echo(typer.style("One or more checks FAILED.", fg=typer.colors.RED))
        raise typer.Exit(code=1)


@app.command()
def contacts_sync(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Force a fresh PBAP pull from the iPhone (rebuilds the contacts cache)."""
    _setup_logging(verbose)

    from blueferry.client import BackendClient, BackendError

    try:
        n = BackendClient().sync_contacts()
    except BackendError as error:
        typer.echo(typer.style(f"Contact sync failed: {error}", fg=typer.colors.RED))
        raise typer.Exit(code=3) from None
    typer.echo(f"Pulled contacts; {n} cached destinations")


@app.command("pair-setup")
def pair_setup(
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Don't wait for profile verification at the end"
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Print detailed Bluetooth pairing and connection activity",
    ),
):
    """First-run wizard: pick a paired iPhone, write the local config,
    walk through the iPhone-side toggle steps."""
    if debug:
        _setup_logging(True)

    from blueferry.pairing_cli import run_wizard

    raise typer.Exit(code=run_wizard(verify_after=not no_verify))


@app.command("pairing-devices-json", hidden=True)
def pairing_devices_json(
    scan_seconds: int = typer.Option(0, "--scan-seconds", min=0, max=30),
) -> None:
    """List Bluetooth devices for graphical setup clients."""
    import json

    from blueferry.bluetooth_devices import iphone_candidates
    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        setup = SetupClient()
        configuration = setup.configuration()
        devices = iphone_candidates(
            setup.devices(scan_seconds=scan_seconds),
            configured_mac=configuration.mac,
            include_unpaired=scan_seconds > 0,
        )
        typer.echo(json.dumps([device.to_dict() for device in devices]))
    except PairingError as error:
        typer.echo(json.dumps({"error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("pairing-bluez-status-json", hidden=True)
def pairing_bluez_status_json() -> None:
    """Report whether the packaged BlueZ bearer API is active."""
    import json

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        typer.echo(json.dumps(SetupClient().bluez_status().to_dict()))
    except PairingError as error:
        typer.echo(json.dumps({"active": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("pairing-compatibility-json", hidden=True)
def pairing_compatibility_json() -> None:
    """Report controller capabilities for graphical setup clients."""
    import json

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        typer.echo(json.dumps(SetupClient().compatibility().to_dict()))
    except PairingError as error:
        typer.echo(json.dumps({"hardware_supported": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("pairing-configuration-json", hidden=True)
def pairing_configuration_json() -> None:
    """Report whether first-run configuration exists without activation."""
    import json

    from blueferry.setup_client import SetupClient

    typer.echo(json.dumps(SetupClient().configuration().to_dict()))


@app.command("pairing-activate-bluez", hidden=True)
def pairing_activate_bluez() -> None:
    """Activate the package's BlueZ drop-in through Polkit."""
    import json

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        value = SetupClient().activate_bluez().to_dict()
        typer.echo(json.dumps({"ok": True, **value}))
    except PairingError as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("pairing-complete", hidden=True)
def pairing_complete(
    mac: str,
    interactive_agent: bool = typer.Option(False, "--interactive-agent", hidden=True),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Pair and perform all Linux-side setup for graphical clients."""
    import json
    import sys

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    if debug:
        _setup_logging(True)

    def emit(event: dict) -> None:
        print(json.dumps(event), flush=True)

    def confirm(passkey: int | None) -> bool:
        emit(
            {
                "event": "confirmation",
                "passkey": f"{passkey:06d}" if passkey is not None else "",
            }
        )
        return sys.stdin.readline().strip().casefold() in {"yes", "y", "accept"}

    def display(passkey: int) -> None:
        emit({"event": "display", "passkey": f"{passkey:06d}"})

    try:
        result = SetupClient().complete(
            mac,
            confirmation=confirm if interactive_agent else None,
            display=display if interactive_agent else None,
        )
        emit(result.to_dict())
    except PairingError as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("pairing-forget", hidden=True)
def pairing_forget(mac: str) -> None:
    """Unpair the phone and clear it from BlueFerry configuration."""
    import json

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        SetupClient().forget(mac)
        typer.echo(json.dumps({"ok": True, "mac": mac.upper()}))
    except PairingError as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


def _json_client():
    """Connect to the backend for machine-readable desktop-shell commands."""
    from blueferry.client import BackendClient, BackendError

    return BackendClient(), BackendError


@app.command("backend-ensure", hidden=True)
def backend_ensure() -> None:
    """Start the packaged backend and replace an old running release."""
    import json

    from blueferry.backend_lifecycle import (
        BackendLifecycleError,
        ensure_backend_current,
    )

    try:
        status = ensure_backend_current()
        typer.echo(json.dumps({"ok": True, "status": status}, ensure_ascii=False))
    except BackendLifecycleError as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("status-json", hidden=True)
def status_json() -> None:
    """Print backend status JSON (stable helper for shell clients)."""
    import json

    client, error_type = _json_client()
    try:
        from blueferry.backend_lifecycle import ensure_backend_current

        ensure_backend_current()
        typer.echo(json.dumps(client.status().to_dict(), ensure_ascii=False))
    except (error_type, RuntimeError) as error:
        typer.echo(json.dumps({"daemon": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("threads-json", hidden=True)
def threads_json(limit: int = typer.Option(200, "--limit")) -> None:
    """Print correlated conversation JSON for non-Python clients."""
    import json

    client, error_type = _json_client()
    try:
        typer.echo(
            json.dumps(
                [thread.to_dict() for thread in client.threads(limit)],
                ensure_ascii=False,
            )
        )
    except error_type as error:
        typer.echo(json.dumps({"error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("contacts-json", hidden=True)
def contacts_json(query: str = typer.Argument(...)) -> None:
    """Search cached contact destinations for non-Python clients."""
    import json

    client, error_type = _json_client()
    try:
        typer.echo(
            json.dumps(
                [
                    {"name": name, "address": address}
                    for name, address in client.find_contacts(query)
                ],
                ensure_ascii=False,
            )
        )
    except error_type as error:
        typer.echo(json.dumps({"error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("message-send", hidden=True)
def message_send(
    recipient: str = typer.Argument(...),
    body: str = typer.Argument(...),
) -> None:
    """Send to an explicit destination for graphical shell clients."""
    client, error_type = _json_client()
    try:
        typer.echo(client.send(recipient, body))
    except error_type as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@app.command("notification-policy-set", hidden=True)
def notification_policy_set(policy: str = typer.Argument(...)) -> None:
    """Set daemon-owned desktop popup policy for shell clients."""
    import json

    client, error_type = _json_client()
    try:
        selected = client.set_notification_policy(policy)
        typer.echo(json.dumps({"ok": True, "policy": selected}))
    except error_type as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


@app.command("storage-policy-set")
def storage_policy_set(policy: str = typer.Argument(...)) -> None:
    """Choose encrypted, unencrypted, or unretained local data."""
    import json

    client, error_type = _json_client()
    try:
        typer.echo(json.dumps(client.set_storage_policy(policy)))
    except error_type as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@app.command("storage-unlock")
def storage_unlock() -> None:
    """Ask the desktop keyring to unlock BlueFerry's retained data."""
    import json

    client, error_type = _json_client()
    try:
        typer.echo(json.dumps(client.unlock_storage()))
    except error_type as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@app.command("thread-send", hidden=True)
def thread_send(
    thread_key: str = typer.Argument(...),
    body: str = typer.Argument(...),
    confirm_group: bool = typer.Option(False, "--confirm-group"),
) -> None:
    """Send through an opaque backend thread key for shell clients."""
    client, error_type = _json_client()
    try:
        typer.echo(client.send_to_thread(thread_key, body, confirm_group=confirm_group))
    except error_type as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@app.command("history-clear")
def history_clear(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Delete local message history and group-correlation metadata."""
    if not yes and not typer.confirm(
        "Delete local BlueFerry history? Nothing on the iPhone is deleted."
    ):
        raise typer.Exit()
    client, error_type = _json_client()
    try:
        client.clear_history()
    except error_type as error:
        typer.echo(f"Could not clear history through the daemon: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo("Local BlueFerry history cleared.")


app.command("sms-list")(sms_list)
app.command("sms-send")(sms_send)


@app.command()
def tui() -> None:
    """Open the interactive terminal messaging client."""
    from blueferry.tui import main as tui_main

    exit_code = tui_main()
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command()
def version():
    """Print version and exit."""
    from blueferry import __version__

    typer.echo(f"BlueFerry {__version__}")


if __name__ == "__main__":
    app()
