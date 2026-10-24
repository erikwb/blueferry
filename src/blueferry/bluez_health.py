"""Notice when bluetoothd stops answering D-Bus, and say why.

On 2026-10-06 bluetoothd sat in uninterruptible sleep in the kernel
(l2cap_chan_connect) and never answered D-Bus again. Every BlueZ call then
waited for its full timeout, BlueFerry logged a stream of NoReply errors, and
the user could not see why nothing connected.

:class:`BluezHealth` decides "unresponsive" only from a cheap asynchronous
``org.freedesktop.DBus.Peer.Ping`` to ``org.bluez``: two consecutive pings,
at least 15 s apart, without a reply. A NoReply/Timeout reported by another
component only triggers an early ping, so one slow call under load (or right
after resume) never flips the state. While unresponsive the
daemon starts no dials or recoveries of its own (they would only queue more
kernel work), publishes ``bluez_unresponsive`` in GetStatus, and logs one
warning. A successful ping or a new ``org.bluez`` owner ends the state.

As a best-effort explanation it reads the state of the bluetoothd process
(``/proc/<pid>/status``, PID from the bus daemon): ``D`` means the kernel is
stuck, and only a reboot helps.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from gi.repository import GLib

log = logging.getLogger(__name__)

NO_REPLY_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.Timeout",
    "org.freedesktop.DBus.Error.TimedOut",
})
# Consecutive unanswered pings, PING_INTERVAL_UNRESPONSIVE_SEC apart.
PING_FAILURE_THRESHOLD = 2
PING_TIMEOUT_SEC = 3.0
PING_INTERVAL_SEC = 30
PING_INTERVAL_UNRESPONSIVE_SEC = 15

# ping(on_reply, on_error): asynchronous Peer.Ping to org.bluez.
Ping = Callable[[Callable[[], None], Callable[[Exception], None]], None]
# owner_pid(on_pid): asynchronous GetConnectionUnixProcessID for org.bluez.
OwnerPid = Callable[[Callable[[int | None], None]], None]


def error_name(error: object) -> str:
    getter = getattr(error, "get_dbus_name", None)
    return str(getter() or "") if callable(getter) else ""


def is_no_reply(error: object) -> bool:
    return error_name(error) in NO_REPLY_ERRORS


def process_state(pid: int, *, proc: Path = Path("/proc")) -> str:
    """The one-letter state of ``pid`` (``D``, ``S``, …) or ``""``."""
    try:
        with open(proc / str(int(pid)) / "status", encoding="utf-8", errors="replace") as stream:
            for line in stream.read(4096).splitlines():
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip()[:1]
    except (OSError, ValueError):
        pass
    return ""


class BluezHealth:
    def __init__(
        self,
        *,
        ping: Ping,
        owner_pid: OwnerPid | None = None,
        read_state: Callable[[int], str] = process_state,
        on_change: Callable[[], None] | None = None,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ping = ping
        self._owner_pid = owner_pid
        self._read_state = read_state
        self._on_change = on_change
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._failures = 0
        self._unresponsive = False
        self._reason = ""
        self._pinging = False
        self._timer: int | None = None
        self._generation = 0

    @property
    def unresponsive(self) -> bool:
        return self._unresponsive

    def snapshot(self) -> dict[str, object]:
        return {
            "bluez_unresponsive": self._unresponsive,
            # "kernel" when bluetoothd is in uninterruptible sleep.
            "bluez_unresponsive_reason": self._reason if self._unresponsive else "",
        }

    def start(self) -> None:
        if self._timer is None:
            self._timer = self._schedule(PING_INTERVAL_SEC, self._tick)

    def stop(self) -> None:
        self._generation += 1
        self._pinging = False
        if self._timer is not None:
            try:
                self._cancel(self._timer)
            except Exception:
                log.debug("could not remove the BlueZ health timer", exc_info=True)
            self._timer = None

    # ---- evidence ------------------------------------------------------------

    def report(self, error: object) -> None:
        """A BlueZ call failed; NoReply/Timeout trigger an early ping."""
        if is_no_reply(error) and not self._unresponsive and self._failures == 0:
            self.probe()

    def report_success(self) -> None:
        """A BlueZ call answered: it is alive (if not stuck, at least responsive)."""
        if self._unresponsive:
            self._failures = 0
            self._recovered("BlueZ answered again")
        elif self._failures:
            self._failures = 0
            self._reschedule(PING_INTERVAL_SEC)

    def owner_changed(self, new_owner: str) -> None:
        """bluetoothd was restarted or went away: start from scratch."""
        self._generation += 1
        self._pinging = False
        self._failures = 0
        if self._unresponsive and new_owner:
            self._recovered("BlueZ was restarted")
            return
        self._reschedule(PING_INTERVAL_SEC)
        if self._unresponsive:
            self._unresponsive = False
            self._reason = ""
            self._notify()

    def probe(self) -> None:
        """Ping now (also used after a component's NoReply)."""
        if self._pinging:
            return
        self._pinging = True
        generation = self._generation

        def replied() -> None:
            if generation != self._generation:
                return
            self._pinging = False
            self.report_success()

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            self._pinging = False
            if is_no_reply(error):
                self._failed()
            elif error_name(error) in (
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner",
            ):
                # Not running is a different problem with its own messages.
                self._failures = 0

        try:
            self._ping(replied, failed)
        except Exception as error:
            self._pinging = False
            log.debug("BlueZ ping could not be sent: %s", type(error).__name__)

    # ---- internals -------------------------------------------------------------

    def _tick(self) -> bool:
        self.probe()
        return True

    def _failed(self) -> None:
        """One ping went unanswered."""
        self._failures += 1
        if self._unresponsive:
            return
        if self._failures < PING_FAILURE_THRESHOLD:
            # Confirm with a second ping after a pause, not right away.
            self._reschedule(PING_INTERVAL_UNRESPONSIVE_SEC)
            return
        self._unresponsive = True
        self._reason = ""
        log.warning(
            "BlueZ (bluetoothd) does not answer D-Bus; pausing BlueFerry's "
            "Bluetooth connection attempts until it responds again",
        )
        self._reschedule(PING_INTERVAL_UNRESPONSIVE_SEC)
        self._explain()
        self._notify()

    def _recovered(self, reason: str) -> None:
        self._unresponsive = False
        self._reason = ""
        log.warning("%s; resuming Bluetooth connection attempts", reason)
        self._reschedule(PING_INTERVAL_SEC)
        self._notify()

    def _reschedule(self, interval: int) -> None:
        if self._timer is None:
            return
        try:
            self._cancel(self._timer)
        except Exception:
            log.debug("could not remove the BlueZ health timer", exc_info=True)
        self._timer = self._schedule(interval, self._tick)

    def _explain(self) -> None:
        if self._owner_pid is None:
            return
        generation = self._generation

        def got_pid(pid: int | None) -> None:
            if generation != self._generation or not self._unresponsive or not pid:
                return
            if self._read_state(pid) == "D":
                self._reason = "kernel"
                log.warning(
                    "bluetoothd is stuck in the kernel (uninterruptible sleep); "
                    "only a reboot will recover Bluetooth",
                )
                self._notify()

        try:
            self._owner_pid(got_pid)
        except Exception:
            log.debug("could not ask for the bluetoothd PID", exc_info=True)

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()


class IntrospectNoReplyFilter(logging.Filter):
    """Let one dbus-python "Introspect error … NoReply" through per window.

    dbus-python logs every failed introspection itself; with a hung
    bluetoothd that is one error line per BlueZ proxy and poll.
    """

    def __init__(self, window_sec: float = 600.0, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self._window = window_sec
        self._clock = clock
        self._last: float | None = None
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Introspect error" not in message or "NoReply" not in message:
            return True
        now = self._clock()
        if self._last is not None and now - self._last < self._window:
            self._suppressed += 1
            return False
        if self._suppressed:
            record.msg = f"{record.msg} (and {self._suppressed} similar since)"
        self._last = now
        self._suppressed = 0
        return True
