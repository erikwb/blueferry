"""Run ``blueferry doctor`` for the settings UIs and return its report.

The check reads BlueZ and the file system and may wait on D-Bus, so clients
run it in a separate process from a worker thread, never on a UI thread.
The report is local diagnostics for the user's own screen (it names the
configured MAC address) and is shown as plain text.
"""
from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
from dataclasses import dataclass

TIMEOUT_SEC = 60.0
MAX_CHARS = 20_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    warnings: bool
    text: str


def run_doctor(timeout: float = TIMEOUT_SEC) -> DoctorReport:
    """*Blocking.* ``blueferry doctor`` in a child process."""
    try:
        completed = subprocess.run(  # nosec B603
            [sys.executable, "-m", "blueferry", "doctor"],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=dict(os.environ, NO_COLOR="1", TERM="dumb"),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reason = type(error).__name__
        return DoctorReport(False, False, f"blueferry doctor did not finish: {reason}")
    return parse_output(completed.returncode, completed.stdout + completed.stderr)


def parse_output(returncode: int, output: str) -> DoctorReport:
    text = _ANSI.sub("", output)
    text = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in text)[:MAX_CHARS]
    return DoctorReport(
        ok=returncode == 0,
        warnings="WARNING" in text or "warnings" in text,
        text=text.strip(),
    )
