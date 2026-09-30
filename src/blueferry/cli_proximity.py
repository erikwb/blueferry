"""`blueferry proximity-lock`: inspect, dry-run, and toggle the away lock."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

import typer

from blueferry import proximity_lock as pl
from blueferry.client import BackendClient, BackendError

proximity_app = typer.Typer(
    help=(
        "Lock the desktop when the paired iPhone goes away (opt-in). "
        "This is a lock trigger only: BlueFerry never unlocks anything."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)

SECURITY_NOTE = (
    "Proximity lock is a convenience, not an authentication factor. "
    "Bluetooth presence can be relayed or spoofed, so BlueFerry only locks "
    "when the phone leaves and never unlocks when it returns."
)

_STATE_TEXT = {
    pl.STATE_DISABLED: "disabled",
    pl.STATE_IDLE: "enabled, waiting to see the iPhone connected",
    pl.STATE_ARMED: "armed (iPhone connected)",
    pl.STATE_GRACE: "iPhone disconnected, grace period running",
    pl.STATE_LOCKED: "locked once; waiting for the iPhone to return",
}
_INHIBIT_TEXT = {
    pl.INHIBIT_SLEEP: "system suspend",
    pl.INHIBIT_ADAPTER_OFF: "desktop Bluetooth is off",
    pl.INHIBIT_DISCOVERING: "Bluetooth discovery or pairing in progress",
    pl.INHIBIT_RECOVERY: "BlueFerry is recovering the Bluetooth adapter",
    pl.INHIBIT_FORGOTTEN: "the iPhone was forgotten",
    pl.INHIBIT_STOPPED: "the service is stopping",
}


def _client() -> BackendClient:
    return BackendClient()


def _status() -> Mapping[str, Any] | None:
    try:
        return _client().status().to_dict()
    except BackendError:
        return None


def _describe(status: Mapping[str, Any]) -> list[str]:
    state = str(status.get("proximity_lock", ""))
    if state not in pl.STATES:
        return ["The running BlueFerry service does not support proximity lock yet."]
    lines = [
        f"State: {_STATE_TEXT[state]}",
        f"Grace period: {int(status.get('proximity_lock_grace_sec') or 0)} s",
    ]
    if state == pl.STATE_GRACE:
        remaining = int(status.get("proximity_lock_remaining_sec") or 0)
        lines.append(f"Locking in: {remaining} s unless the iPhone reconnects")
    inhibited = [
        _INHIBIT_TEXT.get(reason, reason)
        for reason in str(status.get("proximity_lock_inhibited") or "").split(",")
        if reason
    ]
    if inhibited:
        lines.append("Paused because: " + "; ".join(inhibited))
    result = str(status.get("proximity_lock_last_result") or "")
    if result == pl.RESULT_FAILED:
        lines.append("Last lock attempt: failed (see the daemon log)")
    elif result == pl.RESULT_SCREENSAVER_REQUESTED:
        lines.append("Last lock: requested from the screen locker (no reply yet)")
    elif result:
        lines.append(f"Last lock: via {result}")
    return lines


@proximity_app.callback()
def proximity_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        proximity_status()


@proximity_app.command("status")
def proximity_status() -> None:
    """Show whether the away lock is enabled and armed."""
    status = _status()
    if status is None:
        typer.echo("BlueFerry service is not running.", err=True)
        raise typer.Exit(code=2)
    for line in _describe(status):
        typer.echo(line)


def _bus_name_available(bus: str, name: str) -> bool | None:
    """Read-only presence check for the dry run; never calls Lock."""
    try:
        from blueferry.bus import get_session_bus, get_system_bus

        connection = get_session_bus() if bus == "session" else get_system_bus()
        return bool(connection.name_has_owner(name))
    except Exception:
        return None


def _availability(value: bool | None) -> str:
    if value is None:
        return "could not check"
    return "available" if value else "not running"


@proximity_app.command("test")
def proximity_test() -> None:
    """Dry run: explain what would happen. Never locks the screen."""
    status = _status()
    typer.echo("Dry run — nothing will be locked.")
    typer.echo(SECURITY_NOTE)
    typer.echo("")
    if status is None:
        typer.echo("BlueFerry service is not running, so nothing would happen now.")
    else:
        for line in _describe(status):
            typer.echo(line)
    grace = int((status or {}).get("proximity_lock_grace_sec") or pl.DEFAULT_GRACE_SEC)
    enabled = bool((status or {}).get("proximity_lock_enabled"))
    typer.echo("")
    if not enabled:
        typer.echo(
            "Proximity lock is off. Enable it with "
            "`blueferry proximity-lock enable`."
        )
    typer.echo(
        "When enabled and armed, BlueFerry locks once after the iPhone's "
        f"Bluetooth link stays down for {grace} s, trying in order:"
    )
    for number, step in enumerate(pl.DesktopLocker().plan(), start=1):
        typer.echo(f"  {number}. {step}")
    typer.echo("")
    typer.echo(
        "org.freedesktop.ScreenSaver on the session bus: "
        + _availability(_bus_name_available("session", "org.freedesktop.ScreenSaver"))
    )
    typer.echo(
        "org.freedesktop.login1 on the system bus: "
        + _availability(_bus_name_available("system", "org.freedesktop.login1"))
    )
    typer.echo(
        "It does not lock while suspended, while desktop Bluetooth is off, "
        "during discovery or pairing, after the iPhone is forgotten, or before "
        "the iPhone has been seen connected since the service started."
    )


def _set(enabled: bool, grace: int) -> None:
    try:
        status = _client().set_proximity_lock(enabled, grace)
    except BackendError as error:
        typer.echo(f"Could not change proximity lock: {error}", err=True)
        raise typer.Exit(code=2) from None
    for line in _describe(status):
        typer.echo(line)


@proximity_app.command("enable")
def proximity_enable(
    grace: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--grace",
        min=pl.MIN_GRACE_SEC,
        max=pl.MAX_GRACE_SEC,
        help="Seconds the iPhone must stay disconnected before locking.",
    ),
) -> None:
    """Opt in to locking the desktop when the iPhone goes away."""
    typer.echo(SECURITY_NOTE)
    current = _status() or {}
    selected = grace or int(
        current.get("proximity_lock_grace_sec") or pl.DEFAULT_GRACE_SEC
    )
    _set(True, selected)


@proximity_app.command("disable")
def proximity_disable() -> None:
    """Turn the away lock off."""
    current = _status() or {}
    _set(False, int(current.get("proximity_lock_grace_sec") or pl.DEFAULT_GRACE_SEC))
