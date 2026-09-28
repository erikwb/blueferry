"""CLI helpers for one-time code auto-copy.

There is deliberately no command that shows the last copied code: the
daemon never stores codes, so there is nothing to show and nothing to leak.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

import typer

from blueferry import config
from blueferry.otp import MAX_OTP_MESSAGE_CHARS, extract_otp
from blueferry.otp_clipboard import ClipboardTarget, find_target, supports_sensitive_hint


def status_lines(
    *,
    enabled: bool,
    clear_after_s: int,
    environ: Mapping[str, str],
    find: Callable[[Mapping[str, str]], ClipboardTarget | None] = find_target,
    probe_sensitive: Callable[[str], bool] = supports_sensitive_hint,
) -> tuple[list[str], bool]:
    """Describe auto-copy for this session; return (lines, has_warning)."""
    lines = [
        "One-time code auto-copy: " + ("enabled" if enabled else "disabled"),
        "Clear after: "
        + (f"{clear_after_s} seconds" if clear_after_s else "off (code stays until replaced)"),
    ]
    target = find(environ)
    warning = False
    if target is None:
        lines.append("Clipboard helper: none found (install wl-clipboard, or xclip/xsel on X11)")
        warning = enabled
    else:
        lines.append(f"Clipboard helper: {target.tool} ({target.kind})")
        if target.tool == "wl-copy" and probe_sensitive(target.executable):
            lines.append(
                "Clipboard history: codes are marked sensitive (kept out of Klipper history)"
            )
        else:
            lines.append(
                "Clipboard history: this helper cannot mark codes as sensitive; "
                "clipboard managers may keep them (wl-clipboard 2.3+ can)"
            )
            warning = warning or enabled
    lines.append(
        "The backend service decides with its own environment; restart it after "
        "editing local.env."
    )
    return lines, warning


def otp_status() -> None:
    """Show whether one-time codes are auto-copied and how."""
    lines, _warning = status_lines(
        enabled=config.OTP_AUTOCOPY,
        clear_after_s=config.OTP_CLEAR_SECONDS,
        environ=os.environ,
    )
    for line in lines:
        typer.echo(line)


def otp_check() -> None:
    """Read a message on stdin and show which code auto-copy would take.

    Nothing is copied, stored, or sent to the daemon.
    """
    body = sys.stdin.read(MAX_OTP_MESSAGE_CHARS + 1)
    code = extract_otp(body)
    if code is None:
        typer.echo("No one-time code found.")
        raise typer.Exit(code=1)
    typer.echo(f"Would copy: {code}")
