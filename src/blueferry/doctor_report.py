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
import threading
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _

TIMEOUT_SEC = 60.0
MAX_CHARS = 20_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    warnings: bool
    text: str


def run_doctor(timeout: float = TIMEOUT_SEC, command: list[str] | None = None) -> DoctorReport:
    """*Blocking.* ``blueferry doctor`` in a child process.

    The child is registered until it ends, so :func:`terminate_running` can
    stop it when the client quits instead of waiting for the timeout.
    """
    try:
        process = subprocess.Popen(  # nosec B603
            command or [sys.executable, "-m", "blueferry", "doctor"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(os.environ, NO_COLOR="1", TERM="dumb"),
        )
    except OSError as error:
        return _unfinished(error)
    with _running_lock:
        _running.add(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        return _unfinished(error)
    finally:
        with _running_lock:
            _running.discard(process)
    return parse_output(process.returncode, stdout + stderr)


def _unfinished(error: Exception) -> DoctorReport:
    return DoctorReport(False, False, f"blueferry doctor did not finish: {type(error).__name__}")


def terminate_running() -> None:
    """Stop every doctor run of this process, e.g. when the client quits.

    The worker thread waiting in :func:`run_doctor` then returns at once, so
    interpreter exit does not wait up to the timeout for it.
    """
    with _running_lock:
        processes = list(_running)
    for process in processes:
        try:
            process.terminate()
        except OSError:
            pass


def parse_output(returncode: int, output: str) -> DoctorReport:
    text = _ANSI.sub("", output)
    text = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in text)[:MAX_CHARS]
    return DoctorReport(
        ok=returncode == 0,
        warnings="WARNING" in text or "warnings" in text,
        text=text.strip(),
    )


# The settings page's view of the report, the same map for every client.

def idle_state() -> dict[str, Any]:
    return {"running": False, "ran": False, "ok": False, "warnings": False, "text": ""}


def running_state(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "running": True}


def finished_state(state: dict[str, Any], report: object) -> dict[str, Any]:
    """The worker's answer: a :class:`DoctorReport` replaces the state;
    anything else only ends the run, so the page never spins forever."""
    if isinstance(report, DoctorReport):
        return {"running": False, "ran": True, "ok": report.ok,
                "warnings": report.warnings, "text": report.text}
    return {**state, "running": False}


def failed_state(message: str) -> dict[str, Any]:
    return {"running": False, "ran": True, "ok": False, "warnings": False, "text": message}


def summary(state: dict[str, Any]) -> tuple[str, str]:
    """``(level, text)`` for a finished run; level is ``error``, ``warning``
    or ``ok``."""
    if state.get("ok") is not True:
        return "error", _("One or more checks failed.")
    if state.get("warnings") is True:
        return "warning", _("Checks completed with warnings.")
    return "ok", _("All checks passed.")


def view(state: dict[str, Any]) -> dict[str, Any]:
    """``state`` plus the ``level`` and ``summary`` of :func:`summary`."""
    level, text = summary(state)
    return {**state, "level": level, "summary": text}
