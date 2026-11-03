"""``blueferry media``: iPhone now-playing status and media commands."""
from __future__ import annotations

import json
from typing import Optional

import typer

from blueferry.ams.constants import COMMAND_NAMES
from blueferry.client import BackendClient, BackendError
from blueferry.media_volume_view import (
    VOLUME_UP,
    volume_command,
    volume_level,
    volume_supported,
)
from blueferry.text_safety import terminal_text

_SETTINGS = ("enable", "disable", "enable-mpris", "disable-mpris")
_ACTIONS = ("status", *_SETTINGS, "volume", *sorted(COMMAND_NAMES))
_VOLUME_STEPS = ("volume-up", "volume-down")

_DETAILS = {
    "disabled": (
        "iPhone media control is off. Turn it on with 'blueferry media enable' "
        "or in a graphical client's iPhone settings."
    ),
    "requires-notification-access-mode": (
        "iPhone media control needs the Bluetooth LE link, which the "
        "compatibility pairing mode does not use."
    ),
    "le-link-state-unknown": (
        "BlueZ does not report the iPhone's Bluetooth LE link state, which "
        "media control waits for. It needs BlueZ 5.86 or newer with the "
        "bearer API; older BlueZ is not supported for media control yet."
    ),
    "waiting-for-iphone": (
        "Waiting for the iPhone's media service on the Bluetooth LE link."
    ),
}


def _unavailable(snapshot: dict) -> str:
    detail = str(snapshot.get("detail") or "")
    return _DETAILS.get(detail, "iPhone media control is not available.")


def _clock(seconds: object) -> str:
    if not isinstance(seconds, int | float) or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _section(snapshot: dict, key: str) -> dict:
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


def render_now_playing(snapshot: dict) -> list[str]:
    """Human-readable lines; every remote string is terminal-escaped."""
    if not snapshot.get("available"):
        return [_unavailable(snapshot)]
    player, track, queue = (_section(snapshot, key) for key in ("player", "track", "queue"))
    state = str(player.get("state") or "stopped")
    title = track.get("title")
    if not title and not player.get("state"):
        lines = ["Nothing is playing on the iPhone."]
    else:
        heading = terminal_text(title or "Unknown title")
        artist, album = track.get("artist"), track.get("album")
        if artist:
            heading += f" — {terminal_text(artist)}"
        if album:
            heading += f" ({terminal_text(album)})"
        lines = [f"{state.capitalize()}: {heading}"]
        lines.append(
            f"Position: {_clock(track.get('elapsed'))} / {_clock(track.get('duration'))}"
        )
    details = []
    if player.get("name"):
        details.append(f"Player: {terminal_text(player['name'])}")
    volume = player.get("volume")
    if isinstance(volume, int | float):
        details.append(f"Volume: {round(volume * 100)}%")
    index, count = queue.get("index"), queue.get("count")
    if isinstance(index, int) and isinstance(count, int) and count:
        details.append(f"Queue: {index + 1} of {count}")
    if details:
        lines.append("   ".join(details))
    commands = snapshot.get("supported_commands")
    if snapshot.get("supported_commands_known") is False:
        # Older backends omit the key; their list is always the known one.
        lines.append("Commands: unknown (the iPhone has not sent its list yet)")
    elif isinstance(commands, list):
        lines.append(
            "Commands: " + (", ".join(terminal_text(c) for c in commands) or "none")
        )
    return lines


def _volume_status(client: BackendClient, as_json: bool) -> None:
    snapshot = client.now_playing()
    level = volume_level(snapshot)
    if as_json:
        typer.echo(json.dumps({"available": snapshot.get("available") is True, "level": level}))
    elif not snapshot.get("available"):
        typer.echo(_unavailable(snapshot))
    elif level is None:
        typer.echo("iPhone volume: unknown (the iPhone has not reported it yet)")
    else:
        typer.echo(f"iPhone volume: {level}%")
    if not snapshot.get("available"):
        raise typer.Exit(code=1)


def _volume_step(client: BackendClient, command: str) -> None:
    """One relative iPhone volume step; AMS has no absolute volume."""
    snapshot = client.now_playing()
    if not snapshot.get("available"):
        typer.echo(_unavailable(snapshot), err=True)
        raise typer.Exit(code=2)
    if not volume_supported(snapshot, command):
        typer.echo("The current iPhone player does not offer volume control.", err=True)
        raise typer.Exit(code=2)
    level = volume_level(snapshot)
    client.send_media_command(command)
    word = "up" if command == VOLUME_UP else "down"
    typer.echo(
        f"Turned the iPhone volume {word} (was {level}%)."
        if level is not None else f"Turned the iPhone volume {word}."
    )


def _mpris_message(result: dict) -> str:
    if not result.get("media_mpris_enabled"):
        return "The iPhone is not published as an MPRIS player."
    lines = [
        "The iPhone is published as an MPRIS player while it plays. Every "
        "application in your login session can read the track details."
    ]
    if not result.get("media_control_enabled"):
        lines.append("It takes effect once media control is on (blueferry media enable).")
    return "\n".join(lines)


def media(
    action: str = typer.Argument(
        "status",
        help=(
            "status, enable, disable, enable-mpris, disable-mpris, volume, "
            "or one of: " + ", ".join(sorted(COMMAND_NAMES))
        ),
        show_default=True,
    ),
    direction: Optional[str] = typer.Argument(  # noqa: UP045 - typer needs Optional here
        None, help="With 'volume': up or down (one iPhone volume step)",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the raw status JSON"),
) -> None:
    """Show the iPhone's now-playing track, send a media command, step the volume, or opt in/out."""
    selected = action.strip().casefold()
    if selected not in _ACTIONS:
        typer.echo(
            f"Unknown media action. Choose one of: {', '.join(_ACTIONS)}", err=True
        )
        raise typer.Exit(code=2)
    step: str | None = selected if selected in _VOLUME_STEPS else None
    if selected == "volume" and direction is not None:
        step = volume_command(direction)
        if step is None:
            typer.echo("Choose 'blueferry media volume up' or 'volume down'.", err=True)
            raise typer.Exit(code=2)
    elif direction is not None:
        typer.echo("Only 'blueferry media volume' takes up or down.", err=True)
        raise typer.Exit(code=2)
    client = BackendClient()
    try:
        if selected in _SETTINGS:
            enable = selected.startswith("enable")
            if selected.endswith("-mpris"):
                result = client.set_mpris_player(enable)
                message = _mpris_message(result)
            else:
                result = client.set_media_control(enable)
                message = (
                    "iPhone media control is on; it starts when the iPhone's "
                    "LE link is up."
                    if result.get("media_control_enabled")
                    else "iPhone media control is off."
                )
            typer.echo(
                json.dumps(result, ensure_ascii=False, indent=2) if as_json else message
            )
            return
        if selected == "status":
            snapshot = client.now_playing()
            if as_json:
                typer.echo(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return
            for line in render_now_playing(snapshot):
                typer.echo(line)
            if not snapshot.get("available"):
                raise typer.Exit(code=1)
            return
        if step is not None:
            _volume_step(client, step)
        elif selected == "volume":
            _volume_status(client, as_json)
        else:
            client.send_media_command(selected)
    except BackendError as error:
        typer.echo(terminal_text(error), err=True)
        raise typer.Exit(code=2) from None
