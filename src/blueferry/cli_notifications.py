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
sync_app = typer.Typer(
    add_completion=False,
    help=(
        "Close a desktop popup when its notification is removed or "
        "dismissed on the iPhone (opt-in). Without a command, shows the "
        "setting."
    ),
)
notifications_app.add_typer(sync_app, name="sync")


@open_map_app.callback(invoke_without_command=True)
def _open_map_default(context: typer.Context) -> None:
    if context.invoked_subcommand is None:
        open_map_list()


def _client():
    # Imported lazily so `--help` works without D-Bus.
    from blueferry.client import BackendClient

    return BackendClient()


def describe_sync(status: dict) -> list[str]:
    if "mirror_iphone_removals" not in status:
        return ["The running BlueFerry service does not support this setting yet."]
    enabled = status.get("mirror_iphone_removals") is True
    lines = [f"Sync notifications with iPhone: {'on' if enabled else 'off'}"]
    if enabled and status.get("notification_policy") != "all":
        lines.append(
            "Only iPhone app popups are closed; they are shown while desktop "
            "popups are set to All iPhone Notifications."
        )
    return lines


@sync_app.callback(invoke_without_command=True)
def _sync_default(context: typer.Context) -> None:
    if context.invoked_subcommand is None:
        sync_status()


@sync_app.command("status")
def sync_status() -> None:
    """Show whether removals on the iPhone close the desktop popup."""
    from blueferry.client import BackendError

    try:
        status = _client().status().to_dict()
    except BackendError:
        typer.echo("BlueFerry service is not running.", err=True)
        raise typer.Exit(code=2) from None
    for line in describe_sync(status):
        typer.echo(line)


def _set_sync(enabled: bool) -> None:
    from blueferry.client import BackendError

    try:
        _client().set_mirror_notification_removals(enabled)
    except BackendError as error:
        typer.echo(
            f"Could not change notification sync: {terminal_text(error)}", err=True
        )
        raise typer.Exit(code=2) from None
    sync_status()


@sync_app.command("enable")
def sync_enable() -> None:
    """Close popups whose notification was removed on the iPhone."""
    _set_sync(True)


@sync_app.command("disable")
def sync_disable() -> None:
    """Leave popups until they expire, as before."""
    _set_sync(False)


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
