"""One-time code CLI commands without a daemon or clipboard."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from blueferry import cli, cli_otp
from blueferry.cli_otp import status_lines
from blueferry.client import BackendError
from blueferry.otp_clipboard import ClipboardTarget


def test_otp_check_reports_the_code_it_would_copy() -> None:
    result = CliRunner().invoke(
        cli.app, ["otp", "check"], input="Ihr Bestätigungscode lautet: 482913\n"
    )
    assert result.exit_code == 0
    assert "Would copy: 482913" in result.output


def test_earlier_command_names_still_work() -> None:
    result = CliRunner().invoke(
        cli.app, ["otp-check"], input="Ihr Bestätigungscode lautet: 482913\n"
    )
    assert result.exit_code == 0
    assert "Would copy: 482913" in result.output


def test_otp_check_fails_without_a_code() -> None:
    result = CliRunner().invoke(cli.app, ["otp", "check"], input="See you at 18:30\n")
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


def test_disabled_status_never_looks_for_or_probes_a_helper() -> None:
    def fail(*_args):
        raise AssertionError("no helper lookup or wl-copy probe while off")

    lines, warning = status_lines(
        enabled=False, clear_after_s=0, environ={}, find=fail, probe_sensitive=fail,
    )
    assert warning is False
    assert lines[0] == "One-time code auto-copy: disabled"
    assert "otp enable" in lines[1]


def test_status_distinguishes_failed_probe_from_unsupported_helper() -> None:
    lines, warning = status_lines(
        enabled=True, clear_after_s=0, environ={},
        find=lambda _environ: ClipboardTarget("wayland", "wl-copy", "/usr/bin/wl-copy"),
        probe_sensitive=lambda _executable: None,
    )
    assert warning is True
    assert any("probe failed" in line for line in lines)
    assert not any("cannot mark" in line for line in lines)


class _Client:
    def __init__(self, *, enabled=False, fail=False) -> None:
        self.enabled = enabled
        self.fail = fail
        self.calls = []

    def status(self):
        if self.fail:
            raise BackendError("not running")
        return SimpleNamespace(otp_autocopy=self.enabled)

    def set_otp_autocopy(self, enabled):
        self.calls.append(enabled)
        if self.fail:
            raise BackendError("not running")
        return {"otp_autocopy": enabled}


@pytest.mark.parametrize(("command", "expected"), [("enable", True), ("disable", False)])
def test_enable_and_disable_save_through_the_daemon(monkeypatch, command, expected) -> None:
    client = _Client()
    monkeypatch.setattr(cli_otp, "BackendClient", lambda: client)

    result = CliRunner().invoke(cli.app, ["otp", command])

    assert result.exit_code == 0, result.output
    assert client.calls == [expected]
    assert ("enabled" if expected else "disabled") in result.output


def test_enable_without_a_daemon_fails_clearly(monkeypatch) -> None:
    monkeypatch.setattr(cli_otp, "BackendClient", lambda: _Client(fail=True))

    result = CliRunner().invoke(cli.app, ["otp", "enable"])

    assert result.exit_code == 3
    assert "Could not change the setting" in result.output


def test_status_prefers_the_daemon_and_falls_back_to_the_saved_setting(
    isolated_state, monkeypatch,
) -> None:
    from blueferry import config
    from blueferry.otp_settings import OtpSettings

    monkeypatch.setattr(config, "OTP_AUTOCOPY", False)
    assert cli_otp.effective_enabled(lambda: _Client(enabled=True)) is True
    assert cli_otp.effective_enabled(lambda: _Client(fail=True)) is False
    OtpSettings().set(True)
    assert cli_otp.effective_enabled(lambda: _Client(fail=True)) is True
