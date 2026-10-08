"""CLI presentation of the opt-in iPhone call history."""
from __future__ import annotations

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.limits import MAX_CALL_HISTORY_QUERY_LIMIT
from blueferry.models import CallHistoryEntry
from blueferry.text_safety import terminal_text

_MARKERS = {
    "missed": ("✗ missed  ", typer.colors.RED),
    "incoming": ("↙ incoming", typer.colors.GREEN),
    "outgoing": ("↗ outgoing", typer.colors.BLUE),
}


def _render(entry: CallHistoryEntry) -> None:
    label, color = _MARKERS.get(entry.direction, (entry.direction, typer.colors.WHITE))
    caller = terminal_text(entry.display_caller).replace("\n", " ")
    detail = ""
    if entry.name and entry.address:
        detail = typer.style(
            "  " + terminal_text(entry.address).replace("\n", " "), dim=True,
        )
    when = terminal_text(entry.display_time).replace("\n", " ")
    typer.echo(
        f"{typer.style(f'{when:>24s}', dim=True)}  "
        f"{typer.style(label, fg=color)}  "
        f"{typer.style(caller, bold=True)}{detail}"
    )


PRIVACY_NOTE = (
    "Call history keeps who called you and when, under your local storage "
    "policy. It uses the iPhone's Sync Contacts permission and never places, "
    "answers, or listens to calls."
)

call_history_app = typer.Typer(
    help=(
        "Recent iPhone calls (opt-in). Without a subcommand, list them; "
        "`enable` and `disable` change the saved preference."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)


@call_history_app.callback()
def call_history_default(
    ctx: typer.Context,
    missed: bool = typer.Option(False, "--missed", help="Only show missed calls"),
    limit: int = typer.Option(20, "-n", "--limit", min=1, help="Max calls to show"),
    sync: bool = typer.Option(
        False, "--sync", help="Pull the latest call lists from the iPhone first",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        call_history_list(missed=missed, limit=limit, sync=sync)


def call_history_list(missed: bool, limit: int, sync: bool) -> None:
    """Show recent iPhone calls (requires the call-history opt-in)."""
    client = BackendClient()
    try:
        if sync:
            client.sync_call_history()
        # Filtering happens here, so fetch enough to fill the page with
        # missed calls even when most recent calls were answered.
        fetch = MAX_CALL_HISTORY_QUERY_LIMIT if missed else limit
        entries = client.call_history(min(max(1, fetch), MAX_CALL_HISTORY_QUERY_LIMIT))
    except BackendError as error:
        typer.echo(typer.style(f"Call history unavailable: {error}", fg=typer.colors.RED))
        raise typer.Exit(code=3) from None
    if missed:
        entries = [entry for entry in entries if entry.missed]
    entries = entries[:limit]
    if not entries:
        typer.echo("No calls retained." if not missed else "No missed calls retained.")
        return
    for entry in entries:
        _render(entry)


def _set(enabled: bool, popups: bool) -> None:
    try:
        status = BackendClient().set_call_history(enabled, popups)
    except BackendError as error:
        typer.echo(f"Could not change call history: {error}", err=True)
        raise typer.Exit(code=2) from None
    if status.get("call_history_enabled") is True:
        state = "on" if status.get("missed_call_notifications") is True else "off"
        typer.echo(f"Call history is on; missed-call popups are {state}.")
    else:
        typer.echo("Call history is off; retained calls were erased.")


@call_history_app.command("enable")
def call_history_enable(
    popups: bool = typer.Option(
        True, "--popups/--no-popups", help="Desktop popups for new missed calls",
    ),
) -> None:
    """Opt in to mirroring the iPhone's recent calls."""
    typer.echo(PRIVACY_NOTE)
    _set(True, popups)


@call_history_app.command("disable")
def call_history_disable() -> None:
    """Turn call history off and erase the retained calls."""
    _set(False, True)
