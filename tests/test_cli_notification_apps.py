"""`blueferry notifications apps|mute|unmute` against a fake backend client."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli_notifications
from blueferry.cli import app
from blueferry.client import BackendError


class _Backend:
    def __init__(self) -> None:
        self.payload = {
            "apps": [
                {"app_id": "com.example.Chat", "app_name": "Chat\x1b[31m", "muted": True,
                 "seen": True, "last_seen": 1},
                {"app_id": "com.example.Bare", "app_name": "", "muted": False,
                 "seen": False, "last_seen": None},
            ],
            "max_muted": 100, "mute_available": True, "all_notifications": True,
            "env_allowlist": False, "env_blocklist": True,
        }
        self.calls: list[tuple] = []

    def notification_apps(self):
        return self.payload

    def set_notification_app_muted(self, app_id, muted):
        self.calls.append((app_id, muted))
        if app_id == "com.example.Fail":
            raise BackendError("muted apps are unavailable while local storage is locked")
        return muted


@pytest.fixture
def backend(monkeypatch):
    fake = _Backend()
    monkeypatch.setattr(cli_notifications, "_client", lambda: fake)
    return fake


def test_cli_lists_apps_with_markers_and_escaped_names(backend) -> None:
    result = CliRunner().invoke(app, ["notifications", "apps"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("muted  Chat") and "(com.example.Chat)" in lines[0]
    assert "\x1b" not in result.output
    assert lines[1].startswith("       com.example.Bare") and "not seen" in lines[1]
    assert "BLUEFERRY_ANCS_APP_BLOCKLIST" in result.output


def test_cli_mute_and_unmute(backend) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["notifications", "mute", "com.example.Chat"]).exit_code == 0
    assert runner.invoke(app, ["notifications", "unmute", "com.example.Chat"]).exit_code == 0
    assert backend.calls == [("com.example.Chat", True), ("com.example.Chat", False)]
    bad = runner.invoke(app, ["notifications", "mute", "not valid"])
    assert bad.exit_code == 2 and len(backend.calls) == 2
    failed = runner.invoke(app, ["notifications", "mute", "com.example.Fail"])
    assert failed.exit_code == 2 and "locked" in failed.output


