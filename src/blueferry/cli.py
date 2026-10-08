"""Typer CLI entrypoints."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

import typer

from blueferry import bluez_setup, config
from blueferry.cli_call_history import call_history_app
from blueferry.cli_calls import calls_app, phone_status
from blueferry.cli_common import setup_logging as _setup_logging
from blueferry.cli_contacts import contacts_photo
from blueferry.cli_media import media
from blueferry.cli_messages import sms_list, sms_send
from blueferry.cli_notification_actions import notification_actions_app
from blueferry.cli_notifications import notifications_app
from blueferry.cli_proximity import proximity_app
from blueferry.notification_policy import NotificationPolicyStore

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


def _check_controller_le(log: logging.Logger, adapter: str) -> bool:
    """Log the controller's LE state; return True when it needs attention."""
    from blueferry import bluetooth_capabilities
    from blueferry.commands import run_command

    if not config.is_valid_adapter(adapter):
        return False
    available, supported, current, _error, _identity = (
        bluetooth_capabilities.controller_settings(adapter, run_command=run_command)
    )
    if not available or "le" not in supported:
        # Missing LE hardware and unreadable settings are reported by pairing.
        return False
    mode = bluetooth_capabilities.bluez_controller_mode()
    if "le" in current:
        log.info("Bluetooth LE enabled on %s  OK", adapter)
        return False
    log.warning("%s", bluetooth_capabilities.le_disabled_issue(mode))
    if mode:
        log.warning("    /etc/bluetooth/main.conf: ControllerMode = %s", mode)
    else:
        log.warning("    /etc/bluetooth/main.conf: ControllerMode not set")
    return True


@app.command()
def doctor(verbose: bool = typer.Option(False, "-v", "--verbose")):
    """Check that all prerequisites are in place."""
    _setup_logging(verbose)
    log = logging.getLogger("doctor")

    ok = True
    warnings = False

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
                "Pair in a graphical client or with blueferry pair-setup.",
                cod,
            )
            warnings = True

    actions_preference = NotificationPolicyStore().ancs_actions
    if actions_preference and config.SHOW_NOTIFICATION_CONTENT:
        actions_state = "enabled"
    elif actions_preference:
        actions_state = "disabled while BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false"
    else:
        actions_state = "disabled"
    log.info("ANCS notification actions: %s", actions_state)

    # Bluetooth LE switched on? (#192)
    if _check_controller_le(log, config.ADAPTER):
        warnings = True

    # State dir writable
    try:
        config.ensure_dirs()
        log.info("State dir writable: %s", config.STATE_DIR)
    except OSError as e:
        log.error("State dir not writable: %s", e)
        ok = False

    if not _doctor_le_bond(log):
        warnings = True

    if not ok:
        typer.echo(typer.style("One or more checks FAILED.", fg=typer.colors.RED))
        raise typer.Exit(code=1)
    if warnings:
        typer.echo(
            typer.style("Checks completed with warnings.", fg=typer.colors.YELLOW)
        )
    else:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))


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
    compatibility_mode: bool = typer.Option(
        False,
        "--compatibility-mode",
        help=(
            "Skip ANCS connection attempts and use MAP/PBAP only "
            "(recommended for iOS 18 or earlier)"
        ),
    ),
    # Ubuntu 24.04's Typer 0.9 requires typing.Optional for nullable CLI options.
    explicit_pairing: Optional[bool] = typer.Option(  # noqa: UP045
        None,
        "--explicit-pairing/--no-explicit-pairing",
        help="Call Pair immediately instead of Connect (default depends on the adapter)",
    ),
):
    """First-run wizard: pick a paired iPhone, write the local config,
    walk through the iPhone-side toggle steps."""
    if debug:
        _setup_logging(True)

    from blueferry.pairing_cli import run_wizard

    code = run_wizard(
        verify_after=not no_verify,
        compatibility_mode=compatibility_mode,
        explicit_pairing=explicit_pairing,
    )
    raise typer.Exit(code=code)


@app.command("pairing-devices-json", hidden=True)
def pairing_devices_json(
    scan_seconds: int = typer.Option(0, "--scan-seconds", min=0, max=30),
    adapter: str = typer.Option("", "--adapter"),
) -> None:
    """List Bluetooth devices for graphical setup clients."""
    import json

    from blueferry.bluetooth_devices import iphone_candidates
    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        setup = SetupClient()
        configuration = setup.configuration()
        selected = adapter.strip() or None
        devices = iphone_candidates(
            setup.devices(scan_seconds=scan_seconds, adapter=selected),
            adapter=selected or "",
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
def pairing_compatibility_json(
    adapter: str = typer.Option("", "--adapter"),
) -> None:
    """Report controller capabilities for graphical setup clients."""
    import json

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        selected = adapter.strip() or None
        typer.echo(json.dumps(SetupClient().compatibility(selected).to_dict()))
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
    """Activate the package's BlueZ drop-in through systemd."""
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
    adapter: str = typer.Option("", "--adapter"),
    replace_saved_mac: str = typer.Option("", "--replace-saved-mac", hidden=True),
    compatibility_mode: bool = typer.Option(
        False, "--compatibility-mode", hidden=True,
    ),
    explicit_pairing: bool = typer.Option(
        False, "--explicit-pairing", hidden=True,
    ),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Pair and perform all Linux-side setup for graphical clients."""
    import json
    import sys

    from blueferry.errors import PairingError
    from blueferry.pairing_types import PairingTransports
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

    def transports_changed(transports: PairingTransports) -> None:
        emit({
            "event": "transports",
            "map": transports.map,
            "pbap": transports.pbap,
            "ancs": transports.ancs,
        })

    try:
        if not interactive_agent:
            raise PairingError(
                "pairing-complete requires an interactive BlueFerry client"
            )
        emit({"event": "confirmation", "passkey": "", "purpose": "bind"})
        if sys.stdin.readline().strip().casefold() not in {
            "yes", "y", "accept"
        }:
            raise PairingError("Binding this phone was not approved")
        setup = SetupClient()
        selected = adapter.strip() or None
        if replace_saved_mac:
            compatibility = setup.compatibility(selected)
            if not compatibility.pairing_ready:
                raise PairingError(compatibility.issue)
            saved = setup.configuration().adapter or None
            setup.prepare_replacement(replace_saved_mac, mac, adapter=saved)
        result = setup.complete(
            mac,
            adapter=selected,
            confirmation=confirm,
            display=display,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
            transports_changed=transports_changed,
        )
        emit(result.to_dict())
    except PairingError as error:
        payload = {"ok": False, "error": str(error)}
        if error.report_path:
            payload["report_path"] = error.report_path
        typer.echo(json.dumps(payload))
        raise typer.Exit(code=2) from None
    except Exception as error:
        from blueferry import quirks_report

        payload = {"ok": False, "error": str(error) or type(error).__name__}
        if report := quirks_report.latest_report():
            payload["report_path"] = str(report)
        typer.echo(json.dumps(payload))
        raise typer.Exit(code=2) from None


@app.command("pairing-issue")
def pairing_issue(
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Print the GitHub URL without opening a browser",
    ),
    print_url: bool = typer.Option(
        False,
        "--print-url",
        hidden=True,
        help="Print only the GitHub new-issue URL",
    ),
) -> None:
    """File a GitHub issue using the latest pairing report."""
    from blueferry import quirks_report

    path = quirks_report.latest_report()
    if path is None:
        typer.echo("No pairing report found. Pair an iPhone first.")
        raise typer.Exit(code=1)
    url = quirks_report.issue_url(path)
    if print_url:
        typer.echo(url)
        return
    typer.echo(f"Pairing report: {path}")
    typer.echo(quirks_report.issue_instructions(path))
    typer.echo(url)
    if no_open:
        return
    if not quirks_report.open_issue_page(url):
        typer.echo("Open that URL in a browser to file the issue.")


@app.command("pairing-forget", hidden=True)
def pairing_forget(
    mac: str,
    adapter: str = typer.Option("", "--adapter"),
    interactive_approval: bool = typer.Option(
        False, "--interactive-approval", hidden=True
    ),
) -> None:
    """Unpair the phone and clear it from BlueFerry configuration."""
    import json
    import sys

    from blueferry.errors import PairingError
    from blueferry.setup_client import SetupClient

    try:
        if not interactive_approval:
            raise PairingError(
                "pairing-forget requires an interactive BlueFerry client"
            )
        print(
            json.dumps({"event": "confirmation", "purpose": "forget"}),
            flush=True,
        )
        if sys.stdin.readline().strip().casefold() not in {
            "yes", "y", "accept"
        }:
            raise PairingError("Unpairing this phone was not approved")
        setup = SetupClient()
        selected = adapter.strip() or setup.configuration().adapter or None
        setup.forget(mac, adapter=selected)
        typer.echo(json.dumps({"ok": True, "mac": mac.upper()}))
    except PairingError as error:
        typer.echo(json.dumps({"ok": False, "error": str(error)}))
        raise typer.Exit(code=2) from None


def _running_backend_status() -> dict | None:
    """Read status from a running backend; never start one for doctor."""
    from blueferry.bus import get_session_bus
    from blueferry.protocol import BUS_NAME

    try:
        if not get_session_bus().name_has_owner(BUS_NAME):
            return None
        status = _backend_client().status(check_compatibility=False)
    except Exception:
        # BackendError, D-Bus failures, or an unreachable session bus.
        logging.getLogger("doctor").debug("backend status unavailable", exc_info=True)
        return None
    return dict(status.extra)


def _doctor_le_bond(log: logging.Logger) -> bool:
    """Report the daemon's stale-LE-bond finding; False means a warning."""
    from blueferry.pairing_diagnostics import le_bond_findings

    status = _running_backend_status()
    if status is None:
        log.info("Backend not running; skipped the iPhone LE link check")
        return True
    findings = le_bond_findings(status)
    reason = findings["last_le_disconnect_reason"] or "not reported"
    if not findings["le_bond_suspect"]:
        log.info(
            "iPhone LE link: no stale-bond pattern "
            "(%d recent short drops, last reason: %s)",
            findings["le_flap_count"],
            reason,
        )
        return True
    log.warning(
        "iPhone LE pairing may be stale: the LE link dropped %d times within "
        "seconds while Classic stayed connected (last reason: %s). If iPhone "
        "notifications (ANCS) never connect, pairing again may help.",
        findings["le_flap_count"],
        reason,
    )
    log.warning(
        "    On the iPhone: Settings > Bluetooth > (i) next to this computer "
        "> Forget This Device"
    )
    log.warning(
        "    On this computer: bluetoothctl remove %s, then pair again",
        config.IPHONE_MAC,
    )
    return False


def _backend_client():
    """Construct the shared backend client without loading D-Bus for --help."""
    from blueferry.client import BackendClient

    return BackendClient()


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


@app.command("storage-policy-set")
def storage_policy_set(policy: str = typer.Argument(...)) -> None:
    """Choose encrypted, unencrypted, or unretained local data."""
    import json

    from blueferry.client import BackendError

    client = _backend_client()
    try:
        typer.echo(json.dumps(client.set_storage_policy(policy)))
    except BackendError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@app.command("storage-unlock")
def storage_unlock() -> None:
    """Ask the desktop keyring to unlock BlueFerry's retained data."""
    import json

    from blueferry.client import BackendError

    client = _backend_client()
    try:
        typer.echo(json.dumps(client.unlock_storage()))
    except BackendError as error:
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
    from blueferry.client import BackendError

    client = _backend_client()
    try:
        client.clear_history()
    except BackendError as error:
        typer.echo(f"Could not clear history through the daemon: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo("Local BlueFerry history cleared.")


app.command("contacts-photo")(contacts_photo)
app.command("sms-list")(sms_list)
app.command("sms-send")(sms_send)
app.add_typer(proximity_app, name="proximity-lock")
app.add_typer(call_history_app, name="call-history")
app.add_typer(notifications_app, name="notifications")
app.command("media")(media)
app.add_typer(notification_actions_app, name="notification-actions")
app.add_typer(calls_app, name="calls")
app.command("phone-status")(phone_status)


@app.command()
def tui() -> None:
    """Open the interactive terminal messaging client."""
    from blueferry.tui_launcher import main as tui_main

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
