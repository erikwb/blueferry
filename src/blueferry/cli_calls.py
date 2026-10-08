"""Optional phone-call CLI (`blueferry calls ...`) over the Calls1 interface."""
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeVar

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.models import CALLS_STATE_TEXT, CallInfo, CallsSnapshot
from blueferry.text_safety import terminal_text

T = TypeVar("T")

calls_app = typer.Typer(
    help="Experimental iPhone calls through oFono (opt-in: 'blueferry calls enable').",
    invoke_without_command=True,
    no_args_is_help=False,
)



def _interactive() -> bool:
    return sys.stdin.isatty()


def _client() -> BackendClient:
    return BackendClient()


def _run(action: Callable[[], T]) -> T:
    try:
        return action()
    except BackendError as error:
        typer.echo(typer.style(f"Call failed: {error}", fg=typer.colors.RED), err=True)
        raise typer.Exit(code=3) from None


def _describe(call: CallInfo) -> str:
    peer = terminal_text(call.display_peer).replace("\n", " ")
    number = terminal_text(call.number)
    suffix = f" ({number})" if number and number != peer else ""
    flags = " [conference]" if call.multiparty else ""
    return f"{call.call_id:<14} {call.state:<10} {call.direction:<9} {peer}{suffix}{flags}"


def _snapshot() -> CallsSnapshot:
    return _run(lambda: _client().calls())


def _only(snapshot: CallsSnapshot, states: set[str], what: str) -> str:
    matching = [call for call in snapshot.calls if call.state in states]
    if len(matching) == 1:
        return matching[0].call_id
    if not matching:
        typer.echo(typer.style(f"No {what} call.", fg=typer.colors.RED), err=True)
    else:
        typer.echo(
            typer.style(f"Several {what} calls; pass a call ID.", fg=typer.colors.RED),
            err=True,
        )
    raise typer.Exit(code=2)


@calls_app.callback()
def calls_list(ctx: typer.Context) -> None:
    """List current calls and whether call control is available."""
    if ctx.invoked_subcommand is not None:
        return
    snapshot = _snapshot()
    typer.echo(f"Calls: {snapshot.state} — {CALLS_STATE_TEXT.get(snapshot.state, '')}".rstrip())
    if not snapshot.calls:
        typer.echo("No active calls.")
        return
    for call in snapshot.calls:
        typer.echo(_describe(call))


def _set_enabled(enabled: bool) -> None:
    status = _run(lambda: _client().set_calls_enabled(enabled))
    state = str(status.get("calls_state") or ("disabled" if not enabled else "unavailable"))
    word = "enabled" if enabled else "disabled"
    typer.echo(typer.style(f"Phone calls {word}.", fg=typer.colors.GREEN))
    if enabled:
        typer.echo(f"Calls: {state} — {CALLS_STATE_TEXT.get(state, '')}".rstrip())


@calls_app.command("enable")
def calls_enable() -> None:
    """Switch phone calls on (keeps the hands-free roles; needs oFono).

    Calls ringing on the iPhone can then be answered here, and their audio
    is routed to this computer while the call runs here.
    """
    _set_enabled(True)


@calls_app.command("disable")
def calls_disable() -> None:
    """Switch phone calls off and release the iPhone's hands-free link."""
    _set_enabled(False)


@calls_app.command("dial")
def calls_dial(
    number: str = typer.Argument(..., help="Number: optional leading +, digits"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Dial without asking first"),
) -> None:
    """Place a call through the iPhone (asks first; emergency numbers are refused)."""
    if not yes:
        if not _interactive():
            typer.echo(
                typer.style("Pass --yes to dial without a terminal.", fg=typer.colors.RED),
                err=True,
            )
            raise typer.Exit(code=2)
        shown = terminal_text(number).replace("\n", " ")
        if not typer.confirm(f"Call {shown} through the iPhone?", default=False):
            typer.echo("Not dialed.")
            raise typer.Exit(code=1)
    call_id = _run(lambda: _client().dial(number))
    typer.echo(typer.style(f"Dialing ({call_id or 'pending'}).", fg=typer.colors.GREEN))


@calls_app.command("answer")
def calls_answer(
    call_id: str = typer.Argument("", help="Call ID; defaults to the ringing call"),
) -> None:
    """Answer the ringing call (a waiting call holds the active one)."""
    selected = call_id or _only(_snapshot(), {"incoming", "waiting"}, "ringing")
    _run(lambda: _client().answer_call(selected))
    typer.echo(typer.style("Answered.", fg=typer.colors.GREEN))


@calls_app.command("hangup")
def calls_hangup(
    call_id: str = typer.Argument("", help="Call ID; defaults to the only call"),
    all_calls: bool = typer.Option(False, "--all", help="Hang up every call"),
) -> None:
    """Hang up a call, or decline a ringing one."""
    if all_calls and call_id:
        typer.echo(
            typer.style("Pass either a call ID or --all, not both.", fg=typer.colors.RED),
            err=True,
        )
        raise typer.Exit(code=2)
    if all_calls:
        _run(lambda: _client().hangup_all_calls())
        typer.echo("Hung up all calls.")
        return
    selected = call_id or _only(
        _snapshot(),
        {"active", "held", "dialing", "alerting", "incoming", "waiting"},
        "current",
    )
    _run(lambda: _client().hangup_call(selected))
    typer.echo("Hung up.")


@calls_app.command("dtmf")
def calls_dtmf(
    digits: str = typer.Argument(..., help="Tones: 0-9, * and #"),
    call_id: str = typer.Option("", "--call", help="Call ID; defaults to the active call"),
) -> None:
    """Send touch tones on the active call."""
    selected = call_id or _only(_snapshot(), {"active"}, "active")
    _run(lambda: _client().send_call_tones(selected, digits))
    typer.echo("Tones sent.")


@calls_app.command("swap")
def calls_swap() -> None:
    """Swap the active and held calls."""
    _run(lambda: _client().swap_calls())
    typer.echo("Swapped.")


@calls_app.command("hold-answer")
def calls_hold_answer() -> None:
    """Hold the active call and answer the waiting one."""
    _run(lambda: _client().hold_and_answer_call())
    typer.echo("Answered the waiting call.")
