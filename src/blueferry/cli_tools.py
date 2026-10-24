"""``blueferry tools``: start the companion tools next to BlueFerry."""
from __future__ import annotations

import typer

from blueferry import companion_tools
from blueferry.companion_tools import System

_CHOICES = ("status", *companion_tools.ACTIONS)


def _system() -> System:
    return companion_tools.default_system()


def tools(
    action: str = typer.Argument(
        "status", help="status, mirror, send, photos, eject, or pair", show_default=True,
    ),
) -> None:
    """Mirror the iPhone screen, send files or open the iPhone's photos.

    mirror starts UxPlay (or stops the one BlueFerry started), send opens
    LocalSend, photos mounts the camera roll over USB with ifuse and opens
    it, eject unmounts it, and pair asks the iPhone to trust this computer.
    status lists which tools are installed.

    Exit status: 0 on success, 1 when the action failed, 2 for an unknown
    action.
    """
    selected = action.strip().casefold()
    if selected not in _CHOICES:
        typer.echo(f"Unknown action {action!r}; use {', '.join(_CHOICES)}.", err=True)
        raise typer.Exit(code=2)
    system = _system()
    if selected == "status":
        for tool in companion_tools.snapshot(system).tools:
            mark = "ready" if tool.enabled else ("unavailable" if tool.installed else "missing")
            typer.echo(f"{tool.key:<7} {mark:<11} {tool.title}: {tool.subtitle}")
        return
    result = companion_tools.perform(system, selected)
    typer.echo(result.message, err=not result.ok)
    if result.needs_pairing and selected != "pair":
        typer.echo("Run 'blueferry tools pair' to ask the iPhone again.", err=True)
    if not result.ok:
        raise typer.Exit(code=1)
