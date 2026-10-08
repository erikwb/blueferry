"""Narrow, testable boundary for executing fixed system commands."""
from __future__ import annotations

import os
import selectors

# This module is the one argv-only command boundary and never invokes a shell.
import subprocess  # nosec B404
import time
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


def read_command_output(
    argv: Sequence[str],
    *,
    timeout: float,
    limit: int,
    env: Mapping[str, str] | None = None,
) -> bytes | None:
    """Run a command and return at most ``limit + 1`` bytes of its stdout.

    For reading data of unknown size (the clipboard, for example) where only
    a short prefix matters: the command is killed once enough output arrived
    or the timeout passed. Returns ``None`` when the command cannot start,
    times out, or exits with an error.
    """
    command = tuple(str(value) for value in argv)
    if not command or not command[0].startswith("/"):
        raise ValueError("commands need an absolute executable path")
    try:
        # Values are passed directly as argv, never interpreted as shell text.
        process = subprocess.Popen(  # nosec B603
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
            close_fds=True,
        )
    except OSError:
        return None
    stdout = process.stdout
    if stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        process.kill()
        process.wait()
        return None
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    complete = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while size <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    break
                chunk = os.read(stdout.fileno(), limit + 1 - size)
                if not chunk:
                    complete = True
                    break
                chunks.append(chunk)
                size += len(chunk)
        if complete:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            return b"".join(chunks) if returncode == 0 else None
        if size > limit:
            return b"".join(chunks)
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stdout.close()


def run_quiet(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Run a command without input or captured output; True on success.

    Unlike :func:`run_command` nothing waits for output pipes, so a command
    that forks a background child (``xclip``) returns once the parent exits.
    """
    command = tuple(str(value) for value in argv)
    if not command or not command[0].startswith("/"):
        raise ValueError("commands need an absolute executable path")
    try:
        # Values are passed directly as argv, never interpreted as shell text.
        process = subprocess.Popen(  # nosec B603
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
            close_fds=True,
        )
    except OSError:
        return False
    try:
        return process.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return False
