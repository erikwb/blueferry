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


def _python(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def test_read_command_output_returns_short_output() -> None:
    output = commands.read_command_output(
        _python("import sys; sys.stdout.write('482913')"), timeout=10, limit=64
    )
    assert output == b"482913"


def test_read_command_output_stops_after_the_limit() -> None:
    # An endless writer is killed once limit + 1 bytes arrived.
    output = commands.read_command_output(
        _python("import sys\nwhile True: sys.stdout.write('x' * 4096)"),
        timeout=10,
        limit=16,
    )
    assert output is not None
    assert output.startswith(b"x")
    assert len(output) > 16


def test_read_command_output_gives_up_on_silence_and_errors() -> None:
    silent = _python("import time; time.sleep(30)")
    assert commands.read_command_output(silent, timeout=0.2, limit=16) is None
    failing = _python("import sys; sys.stdout.write('x'); sys.exit(1)")
    assert commands.read_command_output(failing, timeout=10, limit=16) is None
    assert commands.read_command_output(["/nonexistent/tool"], timeout=1, limit=16) is None
    with pytest.raises(ValueError):
        commands.read_command_output(["relative"], timeout=1, limit=16)


def test_run_quiet_reports_success_and_bounds_waiting() -> None:
    assert commands.run_quiet(_python("pass"), timeout=10)
    assert not commands.run_quiet(_python("raise SystemExit(3)"), timeout=10)
    assert not commands.run_quiet(_python("import time; time.sleep(30)"), timeout=0.2)
    assert not commands.run_quiet(["/nonexistent/tool"], timeout=1)
    with pytest.raises(ValueError):
        commands.run_quiet(["relative"], timeout=1)
def test_input_text_reaches_the_command_stdin(monkeypatch):
    seen = {}

    def run(*_args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(["probe"], 0, "", "")

    monkeypatch.setattr(commands.subprocess, "run", run)

    commands.run_command(["probe"], timeout=2, input_text="")
    assert seen["input"] == ""
    commands.run_command(["probe"], timeout=2)
    assert seen["input"] is None


def test_an_empty_input_text_gives_a_real_empty_pipe():
    """btmgmt (BlueZ 5.72) polls stdin; an empty pipe reads EOF at once."""
    result = commands.run_command(
        ["/bin/sh", "-c", "test -p /dev/stdin && cat"], timeout=5, input_text="",
    )
    assert result.returncode == 0
    assert result.stdout == ""
