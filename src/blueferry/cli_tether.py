"""``blueferry tether``: opt-in internet sharing from the iPhone's hotspot."""
from __future__ import annotations

import json
import time
from dataclasses import asdict

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.tether_status import TetherStatus

_ACTIONS = ("status", "on", "off")
_POLL_SECONDS = 1.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _emit(status: TetherStatus, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(asdict(status), ensure_ascii=False))
        return
    typer.echo(status.summary())


def tether(
    action: str = typer.Argument(
        "status", help="status, on, or off", show_default=True,
    ),
    wait: int = typer.Option(
        60, "--wait", min=0, max=300,
        help="Seconds to wait for on/off to finish; 0 returns immediately.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the state as JSON."),
) -> None:
    """Share the iPhone's Personal Hotspot over Bluetooth (explicit, opt-in).

    Turn on Personal Hotspot on the iPhone first. With NetworkManager the
    connection is configured automatically; otherwise run a DHCP client on
    the reported interface.
    """
    selected = action.strip().casefold()
    if selected not in _ACTIONS:
        typer.echo(f"Unknown action {action!r}; use status, on, or off.", err=True)
        raise typer.Exit(code=2)
    client = BackendClient()
    try:
        if selected == "on":
            status = client.tether_connect()
        elif selected == "off":
            status = client.tether_disconnect()
        else:
            status = client.tether_state()
        if selected != "status" and wait:
            deadline = _clock() + wait
            while not status.settled and _clock() < deadline:
                _sleep(_POLL_SECONDS)
                status = client.tether_state()
    except BackendError as error:
        typer.echo(f"Tethering request failed: {error}", err=True)
        raise typer.Exit(code=2) from None

    _emit(status, as_json)
    if selected == "on" and status.state != "connected":
        raise typer.Exit(code=1)
    if selected == "off" and status.state != "off":
        raise typer.Exit(code=1)
