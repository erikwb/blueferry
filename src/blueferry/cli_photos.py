"""`blueferry photos recent|open`: the newest photos from a photos plugin."""
from __future__ import annotations

from collections.abc import Callable

import typer

from blueferry import desktop_open, photos_view
from blueferry.plugin_api.client import PluginError
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.text_safety import terminal_text

photos_app = typer.Typer(
    help="Recent photos from a photos plugin (for example Immich).",
    no_args_is_help=True,
)


def _plugin() -> PluginManifest | None:
    return photos_view.find_plugin()


def _open_uri(uri: str) -> None:
    desktop_open.open_uri(uri)


_hooks: dict[str, Callable] = {
    "plugin": _plugin, "load": photos_view.load_recent,
    "fetch": photos_view.fetch_original, "open_uri": _open_uri,
}


def _require_plugin() -> PluginManifest:
    manifest = _hooks["plugin"]()
    if manifest is None:
        typer.echo(photos_view.not_installed_hint(), err=True)
        raise typer.Exit(code=2)
    return manifest


@photos_app.command("recent")
def photos_recent(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200, help="How many items."),
) -> None:
    """List the newest photos and videos: id, date, type."""
    snapshot = _hooks["load"](_require_plugin(), limit)
    if not snapshot.ready:
        typer.echo(terminal_text(snapshot.hint), err=True)
        raise typer.Exit(code=1)
    if not snapshot.photos:
        typer.echo("No photos.")
    for photo in snapshot.photos:
        when = photos_view.taken_text(photo) or "-"
        typer.echo(f"{photo.id}  {when}  {photos_view.type_text(photo)}")


@photos_app.command("open")
def photos_open(
    photo_id: str = typer.Argument(..., help="An id from `blueferry photos recent`."),
    print_path: bool = typer.Option(
        False, "--print-path", help="Only download and print the local path.",
    ),
) -> None:
    """Download the original and open it in the default viewer."""
    manifest = _require_plugin()
    try:
        path = _hooks["fetch"](manifest, photo_id)
    except PluginError as error:
        typer.echo(terminal_text(error), err=True)
        raise typer.Exit(code=1) from None
    if print_path:
        typer.echo(str(path))
        return
    _hooks["open_uri"](path.as_uri())
    typer.echo(f"Opened {terminal_text(path.name)}")
