"""Narrow, testable boundary for executing fixed system commands."""
from __future__ import annotations

# This module is the one argv-only command boundary and never invokes a shell.
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence

from blueferry.errors import CommandError


def run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an argv-only command and normalize launch, timeout, and exit errors."""
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
