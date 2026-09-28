"""External command boundary behavior."""
from __future__ import annotations

import subprocess
import sys

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


def test_spawn_command_feeds_stdin_to_an_inert_helper(tmp_path) -> None:
    output = tmp_path / "received"
    script = f"import sys; open({str(output)!r}, 'w').write(sys.stdin.read())"

    process = commands.spawn_command([sys.executable, "-c", script], stdin_text="482913")

    assert process.wait(timeout=10) == 0
    assert output.read_text() == "482913"


def test_spawn_command_requires_an_absolute_executable() -> None:
    with pytest.raises(ValueError):
        commands.spawn_command(["wl-copy"], stdin_text="482913")


def test_spawn_command_normalizes_a_missing_executable(tmp_path) -> None:
    with pytest.raises(CommandError):
        commands.spawn_command([str(tmp_path / "missing")], stdin_text="482913")


def test_spawn_command_rejects_oversized_input_before_starting(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *a, **k: started.append(a))

    with pytest.raises(ValueError):
        commands.spawn_command(
            [sys.executable], stdin_text="x" * (commands.MAX_SPAWN_STDIN_BYTES + 1)
        )

    assert started == []


def test_spawn_command_closes_stdin_when_the_helper_is_gone(monkeypatch) -> None:
    class _Pipe:
        closed = False

        def write(self, _data):
            raise BrokenPipeError

        def close(self):
            self.closed = True
            raise OSError("already closed")

    pipe = _Pipe()

    class _Process:
        stdin = pipe
        killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return -9

    process = _Process()
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *a, **k: process)

    with pytest.raises(CommandError):
        commands.spawn_command([sys.executable], stdin_text="482913")

    assert pipe.closed is True
    assert process.killed is True
