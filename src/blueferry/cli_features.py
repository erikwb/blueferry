"""`blueferry features`: list or flip the switches the settings UIs offer."""
from __future__ import annotations

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.text_safety import terminal_text

features_app = typer.Typer(help="Feature switches stored in settings.json.", no_args_is_help=False)

RESULT_TEXT = {
    "active": "Saved; already active.",
    "restart-required": "Saved; restart the BlueFerry service to apply it.",
    "environment": "Saved, but the process environment sets this variable and wins.",
}


def _client() -> BackendClient:
    return BackendClient()


@features_app.callback(invoke_without_command=True)
def features_list(context: typer.Context) -> None:
    """Without a subcommand: list every switch, its value and where it comes from."""
    if context.invoked_subcommand is not None:
        return
    try:
        snapshot = _client().features()
    except BackendError as error:
        typer.echo(f"Could not read the switches: {terminal_text(error)}", err=True)
        raise typer.Exit(code=2) from None
    for name, entry in sorted(snapshot.items()):
        if not isinstance(entry, dict):
            continue
        value = "on" if entry.get("value") else "off"
        restart = "  (restart pending)" if entry.get("restart_required") else ""
        typer.echo(terminal_text(
            f"{name:<28}{value:<5}{entry.get('source', '')}  "
            f"{entry.get('variable', '')}{restart}"
        ))


def _set(name: str, enabled: bool) -> None:
    try:
        result = _client().set_feature(name, enabled)
    except BackendError as error:
        typer.echo(f"Could not change {terminal_text(name)}: {terminal_text(error)}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(RESULT_TEXT.get(result, terminal_text(result)))


@features_app.command("on")
def feature_on(name: str) -> None:
    """Turn a switch on (applies after a service restart)."""
    _set(name, True)


@features_app.command("off")
def feature_off(name: str) -> None:
    """Turn a switch off (applies after a service restart)."""
    _set(name, False)
