"""``blueferry tether``: opt-in internet sharing from the iPhone's hotspot."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Optional

import typer

from blueferry.client import BackendClient, BackendError
from blueferry.tether_status import TetherStatus

_ACTIONS = ("status", "on", "off", "enable", "disable")
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


_DISABLED_HINT = (
    "Bluetooth tethering is turned off in BlueFerry. Turn it on with "
    "'blueferry tether enable' or with \"Enable Bluetooth tethering\" in the "
    "iPhone settings, then run 'blueferry tether on'."
)


def _configure(
    client: BackendClient, enabled: bool, autoconnect: bool | None,
) -> TetherStatus:
    if autoconnect is None:
        # Keep the saved automatic-tethering choice unless asked to change it.
        autoconnect = client.tether_state().autoconnect
    return client.tether_configure(enabled, autoconnect)


def tether(
    action: str = typer.Argument(
        "status", help="status, on, off, enable, or disable", show_default=True,
    ),
    wait: int = typer.Option(
        60, "--wait", min=0, max=300,
        help="Seconds to wait for on/off/disable to finish; 0 returns immediately.",
    ),
    # Ubuntu 24.04's Typer 0.9 requires typing.Optional for nullable options.
    autoconnect: Optional[bool] = typer.Option(  # noqa: UP045
        None, "--autoconnect/--no-autoconnect",
        help="With enable: also start tethering automatically once messages "
        "are connected (default: keep the saved choice).",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the state as JSON."),
) -> None:
    """Share the iPhone's Personal Hotspot over Bluetooth (explicit, opt-in).

    Tethering is off until you run 'blueferry tether enable' (or tick
    "Enable Bluetooth tethering" in a client). While it is off BlueFerry
    ignores Bluetooth network links entirely and 'on' is refused. 'disable'
    also stops a tether BlueFerry started; one started elsewhere, for
    example from the Plasma network applet, is left alone.

    Turn on Personal Hotspot on the iPhone first. With NetworkManager the
    connection is configured automatically; otherwise run a DHCP client on
    the reported interface.

    Exit status: 0 when the requested state was reached (or, with --wait 0,
    the request was accepted and is in progress); 1 when it was not reached;
    2 when the request was rejected, for example because tethering is turned
    off, the iPhone is not connected, or the backend is unavailable.
    """
    selected = action.strip().casefold()
    if selected not in _ACTIONS:
        typer.echo(
            f"Unknown action {action!r}; use status, on, off, enable, or disable.",
            err=True,
        )
        raise typer.Exit(code=2)
    if autoconnect is not None and selected != "enable":
        typer.echo("--autoconnect/--no-autoconnect only applies to enable.", err=True)
        raise typer.Exit(code=2)
    client = BackendClient()
    try:
        if selected == "on":
            status = _connect(client)
        elif selected == "off":
            status = client.tether_disconnect()
        elif selected == "enable":
            status = _configure(client, True, autoconnect)
        elif selected == "disable":
            status = _configure(client, False, None)
        else:
            status = client.tether_state()
        if selected in {"on", "off", "disable"} and wait:
            deadline = _clock() + wait
            while not status.settled and _clock() < deadline:
                _sleep(_POLL_SECONDS)
                status = client.tether_state()
    except _Disabled:
        typer.echo(_DISABLED_HINT, err=True)
        raise typer.Exit(code=2) from None
    except BackendError as error:
        typer.echo(f"Tethering request failed: {error}", err=True)
        raise typer.Exit(code=2) from None

    _emit(status, as_json)
    if selected in {"status", "enable"}:
        return
    target = "connected" if selected == "on" else "off"
    pending = "connecting" if selected == "on" else "disconnecting"
    if status.state == target or (not wait and status.state == pending):
        return
    raise typer.Exit(code=1)


class _Disabled(Exception):
    """``on`` was refused because the user has not enabled tethering."""


def _connect(client: BackendClient) -> TetherStatus:
    try:
        return client.tether_connect()
    except BackendError:
        # Explain the refusal that needs a different command; the daemon's
        # NotReady covers several causes, so ask for the state to tell them
        # apart. Anything else keeps the daemon's own message.
        try:
            disabled = not client.tether_state().enabled
        except BackendError:
            disabled = False
        if disabled:
            raise _Disabled from None
        raise
