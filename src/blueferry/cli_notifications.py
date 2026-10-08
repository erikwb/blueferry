"""Desktop notification preferences from the command line."""
from __future__ import annotations

import typer

from blueferry.text_safety import terminal_text

notifications_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Desktop notification preferences.",
)
open_map_app = typer.Typer(
    add_completion=False,
    help=(
        "Choose what clicking a mirrored iPhone notification opens: an "
        "http(s) URL or a desktop entry ID, per iPhone app bundle ID. "
        "Without a rule, clicking keeps today's behaviour. Without a "
        "command, lists the rules."
    ),
)
notifications_app.add_typer(open_map_app, name="open-map")


@open_map_app.callback(invoke_without_command=True)
def _open_map_default(context: typer.Context) -> None:
    if context.invoked_subcommand is None:
        open_map_list()


def _client():
    # Imported lazily so `--help` works without D-Bus.
    from blueferry.client import BackendClient

    return BackendClient()


def _print_rules(rules: list[dict[str, str]]) -> None:
    if not rules:
        typer.echo("No notification click rules. Clicking keeps the default behaviour.")
        return
    width = max(len(rule["bundle_id"]) for rule in rules)
    for rule in rules:
        # Stored values are validated ASCII, but escape at the terminal
        # boundary anyway in case the backend is not this release.
        typer.echo(
            f"{terminal_text(rule['bundle_id']).ljust(width)}  "
            f"{terminal_text(rule['kind']):<7}  {terminal_text(rule['target'])}"
        )


@open_map_app.command("list")
def open_map_list() -> None:
    """Show every notification click rule."""
    from blueferry.client import BackendError

    try:
        _print_rules(_client().notification_open_map())
    except BackendError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None


@open_map_app.command("set")
def open_map_set(
    bundle_id: str = typer.Argument(..., help="iPhone app bundle ID, e.g. com.apple.mobilemail"),
    target: str = typer.Argument(
        ..., help="https:// URL or desktop entry ID, e.g. org.mozilla.Thunderbird.desktop",
    ),
) -> None:
    """Open TARGET when a notification from BUNDLE_ID is clicked."""
    from blueferry.client import BackendError
    from blueferry.notification_open_map import (
        DESKTOP_TARGET,
        parse_target,
        validate_bundle_id,
    )

    try:
        # Validate locally first for an immediate, specific message; the
        # backend validates again and remains the authority.
        validate_bundle_id(bundle_id)
        parsed = parse_target(target)
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    try:
        rules = _client().set_notification_open_target(bundle_id, target)
    except BackendError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    if parsed.kind == DESKTOP_TARGET and not _desktop_entry_installed(parsed.value):
        typer.echo(
            f"Warning: {parsed.value} is not installed for this session; the "
            "rule is saved but a click will do nothing until it is.",
            err=True,
        )
    _print_rules(rules)


@open_map_app.command("remove")
def open_map_remove(
    bundle_id: str = typer.Argument(..., help="iPhone app bundle ID"),
) -> None:
    """Remove the click rule for BUNDLE_ID."""
    from blueferry.client import BackendError

    try:
        removed = _client().remove_notification_open_target(bundle_id)
    except BackendError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    if not removed:
        typer.echo("No click rule exists for that bundle ID.", err=True)
        raise typer.Exit(code=1)
    typer.echo("Click rule removed.")


def _desktop_entry_installed(desktop_id: str) -> bool:
    """Best-effort lookup in this session's XDG data directories."""
    try:
        from gi.repository import Gio

        return Gio.DesktopAppInfo.new(desktop_id) is not None
    except Exception:
        return True  # cannot check; do not warn spuriously
