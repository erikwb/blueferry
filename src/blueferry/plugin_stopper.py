"""Stop a plugin process that still runs code from a replaced or removed venv.

Plugins are D-Bus activated and some (servers such as LocalSend or the
Shortcuts bridge) never exit on their own. After an update, a removal or
disabling, the process that owns the plugin's bus name would keep running
the old code from a venv that is gone. :class:`BusPluginStopper` asks the
session bus who owns the name, checks that the owner runs as this user and
that its executable or interpreter arguments lie inside one of the plugin's
old venvs, then sends SIGTERM and waits briefly. It never sends SIGKILL: a
process that ignores SIGTERM is reported and left alone. D-Bus activation
starts the new code on next use.

Every call blocks (bus round trips, the wait), so it runs where the rest of
:mod:`blueferry.plugin_manager` runs: the CLI or a worker thread, never the
GLib main loop. The bus and ``/proc`` are injectable for tests.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from blueferry.i18n import _

STOP_WAIT_SEC = 3.0
BUS_TIMEOUT_SEC = 2.0
_POLL_SEC = 0.1
_NAME_HAS_NO_OWNER = "org.freedesktop.DBus.Error.NameHasNoOwner"

# Outcome states.
NOT_RUNNING = "not-running"
STOPPED = "stopped"
STILL_RUNNING = "still-running"
FOREIGN = "foreign"
FAILED = "failed"
# Disabling a plugin BlueFerry did not install: there is no venv to check a
# process against, so nothing is stopped (see PLUGINS.md, "Disabling").
UNMANAGED = "unmanaged"

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StopOutcome:
    state: str
    pid: int = 0
    reason: str = ""


class PluginStopper(Protocol):
    def __call__(self, bus_name: str, venvs: Sequence[Path]) -> StopOutcome:
        """Stop the owner of ``bus_name`` if it runs from one of ``venvs``."""


class BusDriver(Protocol):
    """The three org.freedesktop.DBus queries the stopper needs."""

    def owner(self, name: str) -> str | None: ...
    def unix_user(self, owner: str) -> int | None: ...
    def process_id(self, owner: str) -> int | None: ...


class DBusPythonDriver:
    """:class:`BusDriver` on this thread's private session-bus connection."""

    def __init__(self, bus: Any = None) -> None:
        if bus is None:
            from blueferry.bus import get_session_bus

            bus = get_session_bus()
        import dbus

        self._dbus = dbus
        self._driver = dbus.Interface(
            bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "org.freedesktop.DBus",
        )

    def _ask(self, method: str, argument: str) -> Any:
        try:
            return getattr(self._driver, method)(argument, timeout=BUS_TIMEOUT_SEC)
        except self._dbus.exceptions.DBusException as error:
            if error.get_dbus_name() == _NAME_HAS_NO_OWNER:
                return None
            raise

    def owner(self, name: str) -> str | None:
        value = self._ask("GetNameOwner", name)
        return None if value is None else str(value)

    def unix_user(self, owner: str) -> int | None:
        value = self._ask("GetConnectionUnixUser", owner)
        return None if value is None else int(value)

    def process_id(self, owner: str) -> int | None:
        value = self._ask("GetConnectionUnixProcessID", owner)
        return None if value is None else int(value)


# Linux 5.3+ with Python 3.9+; absent elsewhere, where kill() is the fallback.
_PIDFD_OPEN: Callable[[int], int] | None = getattr(os, "pidfd_open", None)
_PIDFD_SEND_SIGNAL: Callable[[int, int], None] | None = getattr(
    signal, "pidfd_send_signal", None,
)


def _inside(path: str, roots: Sequence[Path]) -> bool:
    if not path.startswith("/"):
        return False
    candidate = Path(os.path.normpath(path.removesuffix(" (deleted)")))
    return any(candidate.is_relative_to(root) for root in roots)


class BusPluginStopper:
    """Terminate the bus-name owner of a plugin that runs from an old venv."""

    def __init__(
        self,
        *,
        driver: Callable[[], BusDriver] = DBusPythonDriver,
        proc: Path = Path("/proc"),
        uid: Callable[[], int] = os.getuid,
        kill: Callable[[int, int], None] = os.kill,
        pidfd_open: Callable[[int], int] | None = _PIDFD_OPEN,
        pidfd_send_signal: Callable[[int, int], None] | None = _PIDFD_SEND_SIGNAL,
        close: Callable[[int], None] = os.close,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wait: float = STOP_WAIT_SEC,
    ) -> None:
        self._driver = driver
        self._proc = proc
        self._uid = uid
        self._kill = kill
        self._pidfd_open = pidfd_open
        self._pidfd_send_signal = pidfd_send_signal
        self._close = close
        self._clock = clock
        self._sleep = sleep
        self._wait = wait

    def __call__(self, bus_name: str, venvs: Sequence[Path]) -> StopOutcome:
        roots = [Path(os.path.normpath(str(venv))) for venv in venvs]
        if not roots:
            return StopOutcome(NOT_RUNNING)
        try:
            return self._stop(self._driver(), bus_name, roots)
        except Exception as error:  # the install itself succeeded; never fail it here
            reason = type(error).__name__
            log.warning("could not stop the running plugin %s: %s", bus_name, reason)
            return StopOutcome(FAILED, reason=reason)

    def runs_from(self, pid: int, roots: Sequence[Path]) -> bool:
        """Whether the executable, the interpreter or its script is in ``roots``."""
        base = self._proc / str(pid)
        try:
            if _inside(os.readlink(base / "exe"), roots):
                return True
        except OSError:
            pass
        try:
            argv = (base / "cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        # argv[0] is the venv's python or console script; with a console script
        # run through its shebang, argv[1] is the script. Later arguments are data.
        return any(_inside(arg.decode("utf-8", "replace"), roots) for arg in argv[:2] if arg)

    def _stop(self, driver: BusDriver, bus_name: str, roots: Sequence[Path]) -> StopOutcome:
        owner = driver.owner(bus_name)
        if owner is None:
            return StopOutcome(NOT_RUNNING)
        uid, pid = driver.unix_user(owner), driver.process_id(owner)
        if uid is None or pid is None:
            return StopOutcome(NOT_RUNNING)
        if uid != self._uid() or pid <= 1 or pid == os.getpid() or not self.runs_from(pid, roots):
            log.info("the owner of %s does not run from the plugin's venv; left running",
                     bus_name)
            return StopOutcome(FOREIGN, pid)
        if not self._terminate(driver, bus_name, owner, pid):
            return StopOutcome(NOT_RUNNING)
        deadline = self._clock() + self._wait
        while driver.owner(bus_name) == owner:
            if self._clock() >= deadline:
                log.warning("plugin %s (pid %d) ignored SIGTERM for %g seconds; left running",
                            bus_name, pid, self._wait)
                return StopOutcome(STILL_RUNNING, pid)
            self._sleep(_POLL_SEC)
        log.info("stopped plugin %s (pid %d) after an install change", bus_name, pid)
        return StopOutcome(STOPPED, pid)

    def _terminate(self, driver: BusDriver, bus_name: str, owner: str, pid: int) -> bool:
        """SIGTERM ``pid`` while it still owns the name; False if it is gone.

        Holding a pidfd while re-checking the owner closes the window in which
        the PID could be reused by an unrelated process.
        """
        fd: int | None = None
        if self._pidfd_open is not None and self._pidfd_send_signal is not None:
            try:
                fd = self._pidfd_open(pid)
            except ProcessLookupError:
                return False
        try:
            if driver.owner(bus_name) != owner or driver.process_id(owner) != pid:
                return False
            if fd is not None and self._pidfd_send_signal is not None:
                self._pidfd_send_signal(fd, signal.SIGTERM)
            else:
                self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        finally:
            if fd is not None:
                self._close(fd)
        return True


def stop_ok(outcome: StopOutcome | None) -> bool:
    """False when an old process may still run the replaced code."""
    return outcome is None or outcome.state in (NOT_RUNNING, STOPPED, UNMANAGED)


def stop_note(outcome: StopOutcome | None, *, restarts: bool = True) -> str:
    """One sentence for every client about what happened to the old process."""
    if outcome is None or outcome.state == NOT_RUNNING:
        return ""
    if outcome.state == STOPPED:
        return (_("Stopped the running plugin; it restarts on next use.") if restarts
                else _("Stopped the running plugin."))
    if outcome.state == STILL_RUNNING:
        return _("The running plugin (process {pid}) did not exit; it keeps the old code "
                 "until it ends.").format(pid=outcome.pid)
    if outcome.state == FOREIGN:
        return _("Another program owns the plugin's bus name; it was left running.")
    if outcome.state == UNMANAGED:
        return _("BlueFerry did not install this plugin, so it does not stop it: BlueFerry "
                 "no longer calls it or shows its popups, and a running process ends "
                 "when it goes idle.")
    return _("Could not check for a running plugin ({reason}); an old process may keep "
             "running until it ends.").format(reason=outcome.reason)
