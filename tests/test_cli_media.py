"""`blueferry media` presentation with an inert backend client."""
from __future__ import annotations

import json
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from blueferry import cli_media
from blueferry.cli import app
from blueferry.client import BackendError

PLAYING = {
    "enabled": True,
    "available": True,
    "detail": "ready",
    "player": {"name": "Music", "state": "playing", "rate": 1.0, "volume": 0.42},
    "queue": {"index": 2, "count": 12, "shuffle": "off", "repeat": "all"},
    "track": {
        "artist": "Artist\x1b[31m", "album": "Album", "title": "Title",
        "duration": 245.0, "elapsed": 83.4,
    },
    "supported_commands": ["next", "pause", "previous"],
}


class _Backend:
    snapshot: ClassVar[dict] = PLAYING
    sent: ClassVar[list[str]] = []
    fail: ClassVar[str | None] = None

    def now_playing(self) -> dict:
        if self.fail:
            raise BackendError(self.fail)
        return self.snapshot

    def send_media_command(self, command: str) -> None:
        if self.fail:
            raise BackendError(self.fail)
        self.sent.append(command)


@pytest.fixture
def backend(monkeypatch):
    _Backend.snapshot = PLAYING
    _Backend.sent = []
    _Backend.fail = None
    monkeypatch.setattr(cli_media, "BackendClient", _Backend)
    return _Backend


def test_status_renders_now_playing_and_escapes_remote_text(backend) -> None:
    result = CliRunner().invoke(app, ["media"])
    assert result.exit_code == 0
    assert "Playing: Title — Artist" in result.output
    assert "\x1b" not in result.output
    assert "Position: 1:23 / 4:05" in result.output
    assert "Volume: 42%" in result.output
    assert "Queue: 3 of 12" in result.output
    assert "Commands: next, pause, previous" in result.output


def test_status_json_is_the_backend_snapshot(backend) -> None:
    result = CliRunner().invoke(app, ["media", "status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == PLAYING


@pytest.mark.parametrize("detail,expected", [
    ("disabled", "BLUEFERRY_MEDIA_CONTROL_ENABLED=true"),
    ("requires-notification-access-mode", "compatibility pairing mode"),
    ("waiting-for-iphone", "Waiting for the iPhone"),
])
def test_unavailable_status_explains_why(backend, detail, expected) -> None:
    backend.snapshot = {"enabled": detail != "disabled", "available": False, "detail": detail}
    result = CliRunner().invoke(app, ["media", "status"])
    assert result.exit_code == 1
    assert expected in result.output


@pytest.mark.parametrize("action", ["play", "pause", "next", "previous", "volume-up", "volume-down"])
def test_commands_are_forwarded(backend, action) -> None:
    result = CliRunner().invoke(app, ["media", action])
    assert result.exit_code == 0
    assert backend.sent == [action]


def test_unknown_action_is_rejected_before_calling_the_backend(backend) -> None:
    result = CliRunner().invoke(app, ["media", "eject"])
    assert result.exit_code == 2
    assert backend.sent == []


def test_backend_errors_exit_nonzero(backend) -> None:
    backend.fail = "the iPhone does not currently offer this media command"
    result = CliRunner().invoke(app, ["media", "like"])
    assert result.exit_code == 2
    assert "does not currently offer" in result.output


def test_nothing_playing() -> None:
    lines = cli_media.render_now_playing({
        "enabled": True, "available": True, "player": {}, "track": {}, "queue": {},
        "supported_commands": [],
    })
    assert lines[0] == "Nothing is playing on the iPhone."
    assert lines[-1] == "Commands: none"


class _Interface:
    def __init__(self, name: str, calls: list, *, missing: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.missing = missing

    def GetStatus(self, **_kwargs):
        self.calls.append((self.name, "GetStatus"))
        return json.dumps({"api_version": 2})

    def GetNowPlaying(self, **_kwargs):
        import dbus

        self.calls.append((self.name, "GetNowPlaying"))
        if self.missing:
            raise dbus.exceptions.DBusException(
                "no such method", name="org.freedesktop.DBus.Error.UnknownMethod",
            )
        return json.dumps({"enabled": False, "available": False, "detail": "disabled"})

    def SendMediaCommand(self, command, **_kwargs):
        self.calls.append((self.name, f"SendMediaCommand:{command}"))


def test_client_checks_api_generation_before_media_calls() -> None:
    from blueferry.client import BackendClient
    from blueferry.protocol import MEDIA_IFACE, MESSAGES_IFACE

    calls: list = []
    client = BackendClient(interface_factory=lambda name: _Interface(name, calls))
    assert client.now_playing()["enabled"] is False
    client.send_media_command("next")
    assert calls == [
        (MESSAGES_IFACE, "GetStatus"), (MEDIA_IFACE, "GetNowPlaying"),
        (MESSAGES_IFACE, "GetStatus"), (MEDIA_IFACE, "SendMediaCommand:next"),
    ]


def test_client_explains_a_backend_without_media_support() -> None:
    from blueferry.client import BackendClient

    client = BackendClient(
        interface_factory=lambda name: _Interface(name, [], missing=True)
    )
    with pytest.raises(BackendError, match="update BlueFerry"):
        client.now_playing()
