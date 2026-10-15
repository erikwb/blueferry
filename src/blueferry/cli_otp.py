"""``blueferry otp``: status, enable/disable, and a dry-run detector.

There is deliberately no command that shows the last copied code: the
daemon never stores codes, so there is nothing to show and nothing to leak.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

import typer

from blueferry import config
from blueferry.client import BackendClient, BackendError
from blueferry.otp import MAX_OTP_MESSAGE_CHARS, extract_otp
from blueferry.otp_clipboard import ClipboardTarget, find_target, supports_sensitive_hint
from blueferry.otp_settings import OtpSettings

otp_app = typer.Typer(
    help="Copy one-time codes from new messages to the clipboard (opt-in).",
    no_args_is_help=True,
)


def status_lines(
    *,
    enabled: bool,
    clear_after_s: int,
    environ: Mapping[str, str],
    find: Callable[[Mapping[str, str]], ClipboardTarget | None] | None = None,
    probe_sensitive: Callable[[str], bool | None] | None = None,
) -> tuple[list[str], bool]:
    """Describe auto-copy for this session; return (lines, has_warning).

    Defaults resolve at call time so tests can replace the module-level
    helpers; the real probe starts ``wl-copy --help``, so it only runs while
    auto-copy is on.
    """
    find = find or find_target
    probe_sensitive = probe_sensitive or supports_sensitive_hint
    lines = [
        "One-time code auto-copy: " + ("enabled" if enabled else "disabled"),
    ]
    if not enabled:
        lines.append("Turn it on with 'blueferry otp enable' or the clients' settings.")
        return lines, False
    lines.append(
        "Clear after: "
        + (f"{clear_after_s} seconds" if clear_after_s else "off (code stays until replaced)")
    )
    target = find(environ)
    warning = False
    if target is None:
        lines.append("Clipboard helper: none found (install wl-clipboard, or xclip/xsel on X11)")
        warning = True
    else:
        lines.append(f"Clipboard helper: {target.tool} ({target.kind})")
        sensitive = probe_sensitive(target.executable) if target.tool == "wl-copy" else False
        if sensitive is None:
            lines.append(
                "Clipboard history: capability probe failed; auto-copy waits for a successful probe"
            )
            warning = enabled
        elif sensitive:
            lines.append(
                "Clipboard history: codes are marked sensitive (kept out of Klipper history)"
            )
        else:
            lines.append(
                "Clipboard history: this helper cannot mark codes as sensitive; "
                "clipboard managers may keep them (wl-clipboard 2.3+ can)"
            )
            warning = True
    lines.append(
        "The backend service picks the helper with its own environment; restart it "
        "after changing BLUEFERRY_OTP_CLEAR_SECONDS in local.env."
    )
    return lines, warning


def effective_enabled(client: Callable[[], BackendClient] = BackendClient) -> bool:
    """The running daemon's setting, or the saved one when it is not running."""
    try:
        return bool(client().status().otp_autocopy)
    except BackendError:
        return OtpSettings().enabled


@otp_app.command("status")
def otp_status() -> None:
    """Show whether one-time codes are auto-copied and how."""
    lines, _warning = status_lines(
        enabled=effective_enabled(),
        clear_after_s=config.OTP_CLEAR_SECONDS,
        environ=os.environ,
    )
    for line in lines:
        typer.echo(line)


def _set_enabled(enabled: bool) -> None:
    try:
        status = BackendClient().set_otp_autocopy(enabled)
    except BackendError as error:
        typer.echo(typer.style(f"Could not change the setting: {error}", fg=typer.colors.RED),
                   err=True)
        raise typer.Exit(code=3) from None
    word = "enabled" if status.get("otp_autocopy") is True else "disabled"
    typer.echo(typer.style(f"One-time code auto-copy {word}.", fg=typer.colors.GREEN))


@otp_app.command("enable")
def otp_enable() -> None:
    """Copy one-time codes from new messages of unsaved senders to the clipboard."""
    _set_enabled(True)


@otp_app.command("disable")
def otp_disable() -> None:
    """Stop copying codes; a clipboard helper still serving a code is stopped."""
    _set_enabled(False)


@otp_app.command("check")
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
