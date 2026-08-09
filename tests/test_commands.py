"""External command boundary behavior."""
from __future__ import annotations

import subprocess

import pytest

from blueferry import commands
from blueferry.errors import CommandError


def test_nonzero_exit_uses_stderr_as_the_actionable_error(monkeypatch):
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["systemctl"], 1, stdout="", stderr="permission denied\n"
        ),
    )

    with pytest.raises(CommandError, match="permission denied") as caught:
        commands.run_command(["systemctl", "restart", "bluetooth"], timeout=10)

    assert caught.value.returncode == 1
    assert caught.value.argv == ("systemctl", "restart", "bluetooth")


def test_check_false_returns_nonzero_result(monkeypatch):
    expected = subprocess.CompletedProcess(["probe"], 3, "details", "")
    monkeypatch.setattr(commands.subprocess, "run", lambda *_args, **_kwargs: expected)

    assert commands.run_command(["probe"], timeout=2, check=False) is expected
