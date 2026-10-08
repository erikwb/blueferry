"""One-time code CLI commands without a daemon or clipboard."""
from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli
from blueferry.cli_otp import status_lines
from blueferry.otp_clipboard import ClipboardTarget


def test_otp_check_reports_the_code_it_would_copy() -> None:
    result = CliRunner().invoke(
        cli.app, ["otp-check"], input="Ihr Bestätigungscode lautet: 482913\n"
    )
    assert result.exit_code == 0
    assert "Would copy: 482913" in result.output


def test_otp_check_fails_without_a_code() -> None:
    result = CliRunner().invoke(cli.app, ["otp-check"], input="See you at 18:30\n")
    assert result.exit_code == 1
    assert "No one-time code found." in result.output


def test_status_explains_a_missing_helper() -> None:
    lines, warning = status_lines(
        enabled=True, clear_after_s=0, environ={}, find=lambda _environ: None
    )
    assert warning is True
    assert any("none found" in line for line in lines)


def test_status_reports_sensitive_wayland_copy() -> None:
    lines, warning = status_lines(
        enabled=True,
        clear_after_s=45,
        environ={},
        find=lambda _environ: ClipboardTarget("wayland", "wl-copy", "/usr/bin/wl-copy"),
        probe_sensitive=lambda _executable: True,
    )
    assert warning is False
    assert "Clear after: 45 seconds" in lines
    assert any("marked sensitive" in line for line in lines)


def test_status_warns_when_codes_cannot_be_marked_sensitive() -> None:
    _lines, warning = status_lines(
        enabled=True,
        clear_after_s=0,
        environ={},
        find=lambda _environ: ClipboardTarget("x11", "xclip", "/usr/bin/xclip"),
    )
    assert warning is True


def test_disabled_status_never_warns() -> None:
    _lines, warning = status_lines(
        enabled=False, clear_after_s=0, environ={}, find=lambda _environ: None
    )
    assert warning is False


def test_status_distinguishes_failed_probe_from_unsupported_helper() -> None:
    lines, warning = status_lines(
        enabled=True, clear_after_s=0, environ={},
        find=lambda _environ: ClipboardTarget("wayland", "wl-copy", "/usr/bin/wl-copy"),
        probe_sensitive=lambda _executable: None,
    )
    assert warning is True
    assert any("probe failed" in line for line in lines)
    assert not any("cannot mark" in line for line in lines)
