"""Narrow, testable boundary for executing fixed system commands."""
from __future__ import annotations

# This module is the one argv-only command boundary and never invokes a shell.
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence
from contextlib import suppress

from blueferry.errors import CommandError

# PIPE_BUF on Linux: one write of this size to a fresh pipe never blocks.
MAX_SPAWN_STDIN_BYTES = 4096


def run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an argv-only command and normalize launch, timeout, and exit errors.

    ``input_text`` is written to the command's stdin pipe, which is then
    closed. Pass ``""`` to give a command an empty, pollable stdin instead of
    whatever the caller inherited (often ``/dev/null`` under a service).
    """
    command = tuple(str(value) for value in argv)
    if not command or not command[0]:
        raise ValueError("command argv must not be empty")
    try:
        # Values are passed directly as argv, never interpreted as shell text.
        result = subprocess.run(  # nosec B603
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            input=input_text,
        )
    except FileNotFoundError as error:
        raise CommandError(command, f"{command[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise CommandError(
            command,
            f"{' '.join(command)} timed out after {timeout:g} seconds",
        ) from error

    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise CommandError(
            command,
            message or f"{' '.join(command)} exited with status {result.returncode}",
            returncode=result.returncode,
        )
    return result


def spawn_command(
    argv: Sequence[str],
    *,
    stdin_text: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start a long-lived helper and hand it private input on stdin only.

    The caller owns the returned process. Its output is discarded, so a
    helper that keeps running (a clipboard owner, for example) never blocks
    the caller on a pipe. Private data belongs on stdin: argv is visible to
    every local user through ``/proc``.
    """
    command = tuple(str(value) for value in argv)
    if not command or not command[0].startswith("/"):
        raise ValueError("spawned commands need an absolute executable path")
    data = stdin_text.encode("utf-8")
    if len(data) > MAX_SPAWN_STDIN_BYTES:
        # A single pipe write of at most PIPE_BUF bytes never blocks the
        # caller, even when the helper has not started reading yet.
        raise ValueError(f"stdin input is limited to {MAX_SPAWN_STDIN_BYTES} bytes")
    try:
        # Values are passed directly as argv, never interpreted as shell text.
        process = subprocess.Popen(  # nosec B603
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
            close_fds=True,
        )
    except OSError as error:
        raise CommandError(command, f"{command[0]} could not be started") from error
    stdin = process.stdin
    try:
        if stdin is None:  # pragma: no cover - guaranteed by stdin=PIPE
            raise OSError("no input pipe")
        stdin.write(data)
    except OSError as error:
        process.kill()
        process.wait()
        raise CommandError(command, f"{command[0]} exited before reading its input") from error
    finally:
        if stdin is not None:
            with suppress(OSError):
                stdin.close()
    return process
