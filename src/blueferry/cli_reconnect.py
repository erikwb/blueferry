"""`blueferry reconnect`: page the iPhone now instead of waiting for backoff."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Optional

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.reconnect_view import (
    UNREACHABLE_TEXT,
    UNSUPPORTED_TEXT,
    reconnect_view,
    result_text,
)
from blueferry.text_safety import terminal_text

# A BlueZ Connect may take its full 45 s method timeout before it fails.
WAIT_SECONDS = 60
# Replies that end the command at once with an error.
_FAILED = frozenset({"unreachable", "bluez-kernel", "bluez-unresponsive", "call-active"})


def _client() -> BackendClient:
    return BackendClient()


_sleep: Callable[[float], None] = time.sleep


def reconnect(
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Wait until the attempt succeeds or fails.",
    ),
    auto_reset: Optional[bool] = typer.Option(  # noqa: UP045 - typer needs Optional here
        None, "--auto-reset/--no-auto-reset",
        help="Turn the automatic reset of a stuck profile on or off (saved) "
        "instead of reconnecting",
    ),
) -> None:
    """Reconnect the iPhone now, skipping the automatic backoff."""
    client = _client()
    if auto_reset is not None:
        try:
            selected = client.set_auto_profile_reset(auto_reset)
        except BackendError as error:
            typer.echo(f"Could not save the setting: {terminal_text(error)}", err=True)
            raise typer.Exit(code=2) from None
        typer.echo(f"Automatic profile reset {'on' if selected else 'off'}.")
        return
    try:
        result = client.reconnect_phone()
    except BackendError as error:
        message = terminal_text(error)
        if "Unknown method" in message or "UnknownMethod" in message:
            message = UNSUPPORTED_TEXT
        typer.echo(message, err=True)
        raise typer.Exit(code=2) from None
    if result in _FAILED:
        typer.echo(result_text(result), err=True)
        raise typer.Exit(code=1)
    if result in ("connected", "in-progress") or not wait:
        typer.echo(result_text(result))
        return
    typer.echo(result_text(result))
    for _attempt in range(WAIT_SECONDS):
        _sleep(1.0)
        try:
            state = client.status().to_dict().get("phone_reconnect_state")
        except BackendError:
            continue
        if state == "connected":
            typer.echo(result_text("connected"))
            return
        if state == "unreachable":
            typer.echo(UNREACHABLE_TEXT, err=True)
            raise typer.Exit(code=1)
    try:
        typer.echo(reconnect_view(client.status().to_dict()).hint)
    except BackendError as error:
        typer.echo(terminal_text(error), err=True)
