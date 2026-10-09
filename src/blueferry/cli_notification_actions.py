"""`blueferry notification-actions`: show and toggle iPhone action buttons."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from blueferry.client import BackendClient, BackendError

notification_actions_app = typer.Typer(
    help=(
        "Offer iPhone notification actions (Accept, Decline, Clear, ...) as "
        "desktop popup buttons (opt-in). A click runs the action on the iPhone."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)


def _client() -> BackendClient:
    return BackendClient()


def _status() -> Mapping[str, Any] | None:
    try:
        return _client().status().to_dict()
    except BackendError:
        return None


def describe(status: Mapping[str, Any]) -> list[str]:
    if "ancs_actions_preference" not in status:
        return [
            "The running BlueFerry service does not support this setting yet."
        ]
    preference = status.get("ancs_actions_preference") is True
    lines = [f"Notification actions: {'on' if preference else 'off'}"]
    if preference and status.get("notification_content_shown") is False:
        lines.append(
            "Inactive: notification content is hidden "
            "(BLUEFERRY_SHOW_NOTIFICATION_CONTENT=false), and action labels "
            "count as content."
        )
    elif preference and status.get("notification_policy") != "all":
        lines.append(
            "Inactive until desktop popups are set to All iPhone Notifications."
        )
    return lines


@notification_actions_app.callback()
def notification_actions_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        notification_actions_status()


@notification_actions_app.command("status")
def notification_actions_status() -> None:
    """Show whether iPhone action buttons are offered."""
    status = _status()
    if status is None:
        typer.echo("BlueFerry service is not running.", err=True)
        raise typer.Exit(code=2)
    for line in describe(status):
        typer.echo(line)


def _set(enabled: bool) -> None:
    try:
        _client().set_ancs_notification_actions(enabled)
    except BackendError as error:
        typer.echo(f"Could not change notification actions: {error}", err=True)
        raise typer.Exit(code=2) from None
    notification_actions_status()


@notification_actions_app.command("enable")
def notification_actions_enable() -> None:
    """Opt in to iPhone action buttons on desktop popups."""
    _set(True)


@notification_actions_app.command("disable")
def notification_actions_disable() -> None:
    """Turn iPhone action buttons off and close any that are showing."""
    _set(False)
