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

from . import config

TIMEOUT_SEC = 60.0
MAX_CHARS = 20_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    warnings: bool
    text: str


class DoctorRunner:
    """Own a client's doctor children and cancel even a run still starting.

    Stopping is permanent for this owner, so work queued before shutdown
    cannot start a new check afterwards. Other clients/runners are unaffected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False
        self._running: set[subprocess.Popen] = set()

    def run(self, timeout: float = TIMEOUT_SEC, command: list[str] | None = None) -> DoctorReport:
        """*Blocking.* Run on a worker thread, never on a toolkit thread."""
        with self._lock:
            if self._stopped:
                return DoctorReport(False, False, "blueferry doctor was cancelled")
        # config injects file-backed settings into our environment. The child
        # must reread current files, preserving only genuine overrides.
        environment = {
            key: value for key, value in os.environ.items()
            if key not in config.LOCAL_ENV_KEYS or key in config.EXPLICIT_ENV_KEYS
        }
        environment.update(NO_COLOR="1", TERM="dumb")
        try:
            process = subprocess.Popen(  # nosec B603
                command or [sys.executable, "-m", "blueferry", "doctor"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", env=environment,
            )
        except OSError as error:
            return _unfinished(error)
        with self._lock:
            self._running.add(process)
            # stop() may have run while Popen was creating this process.
            if self._stopped:
                _kill(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill(process)
            process.communicate()
            return _unfinished(error)
        finally:
            with self._lock:
                self._running.discard(process)
        return parse_output(process.returncode, stdout + stderr)

    def stop(self) -> None:
        """Cancel this owner's checks without waiting on the UI thread."""
        with self._lock:
            self._stopped = True
            processes = list(self._running)
        for process in processes:
            _kill(process)


def _kill(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except OSError:
        pass  # It may already have exited; the worker still reaps it.


def run_doctor(timeout: float = TIMEOUT_SEC, command: list[str] | None = None) -> DoctorReport:
    """Run a standalone check; UIs use their own stoppable DoctorRunner."""
    return DoctorRunner().run(timeout, command)


def _unfinished(error: Exception) -> DoctorReport:
    return DoctorReport(False, False, f"blueferry doctor did not finish: {type(error).__name__}")


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
