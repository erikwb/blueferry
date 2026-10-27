"""``blueferry tools`` with an injected fake system; nothing is started."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli, cli_tools
from blueferry.companion_tools import CommandResult
from tests.test_companion_tools import DEVICE, PHOTO_TOOLS, Fake

runner = CliRunner()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = Fake(tmp_path, installed={"uxplay", *PHOTO_TOOLS}, answers=DEVICE)
    monkeypatch.setattr(cli_tools, "_system", fake.system)
    return fake


def test_status_lists_every_tool_without_starting_anything(fake) -> None:
    result = runner.invoke(cli.app, ["tools"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert [line.split()[0] for line in lines] == ["mirror", "send", "photos", "eject"]
    assert lines[0].split()[1] == "ready" and lines[1].split()[1] == "missing"
    assert fake.ran == [["idevice_id", "-l"]] and fake.commands == {}


def test_mirror_starts_uxplay(fake) -> None:
    result = runner.invoke(cli.app, ["tools", "mirror"])
    assert result.exit_code == 0 and "Control Center" in result.output
    assert list(fake.commands) == [("uxplay", "-n", "desk", "-nh")]


def test_photos_failure_suggests_pairing_and_exits_one(fake) -> None:
    fake.answers[("idevicepair", "validate")] = CommandResult(1, "not paired")
    result = runner.invoke(cli.app, ["tools", "photos"])
    assert result.exit_code == 1
    assert "Trust This Computer" in result.output and "blueferry tools pair" in result.output


def test_unknown_action_exits_two(fake) -> None:
    result = runner.invoke(cli.app, ["tools", "beam"])
    assert result.exit_code == 2 and "Unknown action" in result.output
