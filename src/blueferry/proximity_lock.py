"""Lock the desktop session after the paired iPhone has been away a while.

This is a convenience lock trigger, not an authentication factor. Bluetooth
presence can be relayed, replayed, or spoofed by anyone who can put a radio
near the desk, so BlueFerry deliberately never *unlocks* anything when the
phone returns. Only the absence of an already-trusted link is acted on, and a
wrong decision in that direction costs the user one password prompt.

Presence comes only from the bearer state that ``BearerSupervisor`` already
polls (BR/EDR and LE ``Connected``). No scanning, no RSSI: BlueZ exposes
``Device1.RSSI`` for Classic devices only during discovery, and running
discovery continuously would disturb the very links BlueFerry keeps up.

Everything here runs on the daemon's GLib loop. Timers, the clock, and the
D-Bus call used for locking are injected so the state machine and the
dispatch order can be tested without a bus or a real clock.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from blueferry import config
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

DEFAULT_GRACE_SEC = 60
MIN_GRACE_SEC = 10
MAX_GRACE_SEC = 3600

STATE_DISABLED = "disabled"
# Enabled but waiting to see the phone (startup, after resume, after an
# inhibitor such as adapter power-off, or after a lock).
STATE_IDLE = "idle"
STATE_ARMED = "armed"
STATE_GRACE = "grace"
STATE_LOCKED = "locked"
STATES = frozenset({
    STATE_DISABLED,
    STATE_IDLE,
    STATE_ARMED,
    STATE_GRACE,
    STATE_LOCKED,
})

# Inhibitors are explicit, non-radio reasons not to treat a lost link as the
# user walking away.
INHIBIT_SLEEP = "sleep"
INHIBIT_ADAPTER_OFF = "adapter-off"
INHIBIT_DISCOVERING = "discovering"
INHIBIT_RECOVERY = "recovery"
INHIBIT_FORGOTTEN = "forgotten"
INHIBIT_STOPPED = "stopped"

RESULT_SCREENSAVER = "screensaver"
# kscreenlocker delays its Lock() reply until the greeter is up. A reply that
# does not arrive in time means the request was delivered and is still being
# handled, so it is reported as requested rather than retried through logind.
RESULT_SCREENSAVER_REQUESTED = "screensaver-requested"
RESULT_LOGIN1 = "login1"
RESULT_FAILED = "failed"

LOCK_CALL_TIMEOUT_SEC = 5.0
SCREENSAVER_LOCK_TIMEOUT_SEC = 30.0
_NO_REPLY_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.Timeout",
    "org.freedesktop.DBus.Error.TimedOut",
})
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]
ReadPresence = Callable[[], bool | None]
LockDone = Callable[[str], None]
# (bus, service, path, interface, method, signature, args, reply, error,
#  *, timeout)
AsyncCall = Callable[..., None]


def clamp_grace(value: object, default: int = DEFAULT_GRACE_SEC) -> int:
    """Return a grace period within the supported bounds."""
    if isinstance(value, bool):
        return default
    try:
        selected = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(MIN_GRACE_SEC, min(selected, MAX_GRACE_SEC))


def presence_from_bearers(bredr: bool | None, le: bool | None) -> bool | None:
    """Collapse the two bearer observations into present/absent/unknown.

    Either live bearer proves presence. Absence needs a concrete Classic
    ``False``; LE may be ``None`` because compatibility mode and some BlueZ
    builds never expose it. A Classic read failure is unknown, never absent.
    """
    if bredr is True or le is True:
        return True
    if bredr is False:
        return False
    return None


class ProximityLockSettings:
    """Persist the user's opt-in in the owner-only settings document.

    ``BLUEFERRY_PROXIMITY_LOCK`` and ``BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC``
    provide the initial values. A value saved through the D-Bus API (Qt
    settings, ``blueferry proximity-lock enable``) takes precedence.
    """

    ENABLED_KEY = "proximity_lock_enabled"
    GRACE_KEY = "proximity_lock_grace_sec"

    def __init__(
        self,
        path: Path | None = None,
        *,
        default_enabled: bool | None = None,
        default_grace: int | None = None,
    ) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        try:
            payload = self._settings.read()
        except OSError:
            payload = {}
        fallback_enabled = (
            config.PROXIMITY_LOCK if default_enabled is None else default_enabled
        )
        fallback_grace = clamp_grace(
            config.PROXIMITY_LOCK_GRACE_SEC if default_grace is None else default_grace
        )
        stored_enabled = payload.get(self.ENABLED_KEY)
        self._enabled = (
            stored_enabled if isinstance(stored_enabled, bool) else bool(fallback_enabled)
        )
        self._grace = clamp_grace(payload.get(self.GRACE_KEY), fallback_grace)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def grace_sec(self) -> int:
        return self._grace

    def set(self, enabled: bool, grace_sec: int) -> tuple[bool, int]:
        if not isinstance(enabled, bool):
            raise ValueError("proximity lock enabled must be a boolean")
        if isinstance(grace_sec, bool) or not isinstance(grace_sec, int):
            raise ValueError("proximity lock grace period must be an integer")
        if not MIN_GRACE_SEC <= grace_sec <= MAX_GRACE_SEC:
            raise ValueError(
                f"proximity lock grace period must be between {MIN_GRACE_SEC} "
                f"and {MAX_GRACE_SEC} seconds"
            )
        self._settings.update(**{
            self.ENABLED_KEY: enabled,
            self.GRACE_KEY: grace_sec,
        })
        self._enabled = enabled
        self._grace = grace_sec
        return enabled, grace_sec


def _dbus_async_call(
    bus: str,
    service: str,
    path: str,
    interface: str,
    method: str,
    signature: str,
    args: tuple,
    reply_handler: Callable[..., None],
    error_handler: Callable[[Exception], None],
    *,
    timeout: float = LOCK_CALL_TIMEOUT_SEC,
) -> None:
    from blueferry.bus import get_session_bus, get_system_bus

    connection = get_session_bus() if bus == "session" else get_system_bus()
    connection.call_async(
        service,
        path,
        interface,
        method,
        signature,
        args,
        reply_handler,
        error_handler,
        timeout=timeout,
    )


def _error_name(error: Exception) -> str:
    getter = getattr(error, "get_dbus_name", None)
    name = getter() if callable(getter) else None
    return str(name or type(error).__name__)[:128]


class DesktopLocker:
    """Ask the desktop to lock, trying the session bus before logind.

    1. ``org.freedesktop.ScreenSaver.Lock`` on the session bus. KDE Plasma
       implements it, and it needs no system-bus policy.
    2. ``org.freedesktop.login1.Session.Lock`` on the system bus for the
       caller's own session (systemd-logind and elogind). The session is
       found with ``GetSessionByPID(own pid)``, then ``XDG_SESSION_ID``, then
       logind's ``session/auto`` alias, which resolves to the user's display
       session for processes (such as a systemd user service) outside one.

    Every call is asynchronous; the GLib loop is never blocked.
    """

    def __init__(
        self,
        call: AsyncCall | None = None,
        *,
        pid: Callable[[], int] = os.getpid,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._call = call or _dbus_async_call
        self._pid = pid
        self._environ = os.environ if environ is None else environ

    def plan(self) -> list[str]:
        """Human-readable dispatch order, for the CLI dry run."""
        steps = [
            "session bus: org.freedesktop.ScreenSaver.Lock()",
            "system bus: org.freedesktop.login1 GetSessionByPID(<daemon pid>) "
            "-> Session.Lock()",
        ]
        if self._session_id():
            steps.append(
                "system bus: org.freedesktop.login1 GetSession($XDG_SESSION_ID) "
                "-> Session.Lock()"
            )
        steps.append(
            "system bus: /org/freedesktop/login1/session/auto Session.Lock()"
        )
        return steps

    def _session_id(self) -> str:
        value = str(self._environ.get("XDG_SESSION_ID", "")).strip()
        return value if _SESSION_ID_RE.fullmatch(value) else ""

    def lock(self, done: LockDone) -> None:
        def failed(error: Exception) -> None:
            if _error_name(error) in _NO_REPLY_ERRORS:
                log.info(
                    "ScreenSaver accepted the lock request but has not "
                    "replied yet; not falling back to logind"
                )
                done(RESULT_SCREENSAVER_REQUESTED)
                return
            log.info(
                "ScreenSaver lock unavailable (%s); trying logind",
                _error_name(error),
            )
            self._lock_login1_by_pid(done)

        self._invoke(
            "session",
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            "Lock",
            "",
            (),
            lambda *_reply: done(RESULT_SCREENSAVER),
            failed,
            timeout=SCREENSAVER_LOCK_TIMEOUT_SEC,
        )

    def _lock_login1_by_pid(self, done: LockDone) -> None:
        def found(path: object) -> None:
            self._lock_login1_session(str(path), done, self._lock_login1_by_env)

        self._invoke(
            "system",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "GetSessionByPID",
            "u",
            (int(self._pid()),),
            found,
            lambda error: self._lock_login1_by_env(done, error),
        )

    def _lock_login1_by_env(self, done: LockDone, error: Exception | None = None) -> None:
        if error is not None:
            log.debug("logind session by PID unavailable: %s", _error_name(error))
        session_id = self._session_id()
        if not session_id:
            self._lock_login1_auto(done)
            return

        def found(path: object) -> None:
            self._lock_login1_session(
                str(path), done, lambda done, _error=None: self._lock_login1_auto(done)
            )

        self._invoke(
            "system",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "GetSession",
            "s",
            (session_id,),
            found,
            lambda _error: self._lock_login1_auto(done),
        )

    def _lock_login1_auto(self, done: LockDone) -> None:
        def failed(error: Exception) -> None:
            log.warning(
                "could not lock the desktop session: %s", _error_name(error)
            )
            done(RESULT_FAILED)

        self._lock_login1_session(
            "/org/freedesktop/login1/session/auto",
            done,
            lambda done, error=None: failed(error or RuntimeError("lock failed")),
        )

    def _lock_login1_session(
        self,
        path: str,
        done: LockDone,
        fallback: Callable[..., None],
    ) -> None:
        if not path.startswith("/org/freedesktop/login1/session/"):
            fallback(done, ValueError("unexpected logind session path"))
            return
        self._invoke(
            "system",
            "org.freedesktop.login1",
            path,
            "org.freedesktop.login1.Session",
            "Lock",
            "",
            (),
            lambda *_reply: done(RESULT_LOGIN1),
            lambda error: fallback(done, error),
        )

    def _invoke(
        self,
        bus: str,
        service: str,
        path: str,
        interface: str,
        method: str,
        signature: str,
        args: tuple,
        reply: Callable[..., None],
        error: Callable[[Exception], None],
        *,
        timeout: float = LOCK_CALL_TIMEOUT_SEC,
    ) -> None:
        try:
            self._call(
                bus, service, path, interface, method, signature, args, reply, error,
                timeout=timeout,
            )
        except Exception as failure:  # connection setup can fail synchronously
            error(failure)


class ProximityLock:
    """Lock once after the phone has been continuously away for the grace period.

    Rules:

    * Only a link that was observed *present* since the last start, resume,
      lock, or inhibitor can arm the lock. Starting the daemon without the
      phone nearby therefore never locks.
    * A return inside the grace period cancels it; the next loss starts a
      full new grace period.
    * After one lock the state stays ``locked`` until the phone is seen
      again. There is no retry loop, even if dispatch failed.
    * Inhibitors (suspend, adapter powered off, discovery or pairing, the
      daemon's own adapter recovery, a forgotten phone, shutdown) cancel any
      pending grace and require fresh presence afterwards.
    * Unknown bearer reads neither arm nor start a grace period.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        grace_sec: int,
        read_presence: ReadPresence,
        locker: DesktopLocker | None = None,
        on_status: Callable[[], None] | None = None,
        schedule: Schedule | None = None,
        cancel: Cancel | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        if schedule is None or cancel is None:
            from gi.repository import GLib

            schedule = schedule or GLib.timeout_add_seconds
            cancel = cancel or GLib.source_remove
        self._enabled = bool(enabled)
        self._grace = clamp_grace(grace_sec)
        self._read_presence = read_presence
        self._locker = locker or DesktopLocker()
        self._on_status = on_status
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._state = STATE_IDLE if self._enabled else STATE_DISABLED
        self._inhibitors: set[str] = set()
        self._absent_since: float | None = None
        self._timer_id: int | None = None
        self._last_result = ""
        self._dispatching = False
        self._generation = 0

    # ---- inspection ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def grace_sec(self) -> int:
        return self._grace

    def snapshot(self) -> dict[str, Any]:
        remaining = 0
        if self._state == STATE_GRACE and self._absent_since is not None:
            remaining = max(
                0, math.ceil(self._absent_since + self._grace - self._clock())
            )
        return {
            "proximity_lock": self._state,
            "proximity_lock_enabled": self._enabled,
            "proximity_lock_grace_sec": self._grace,
            "proximity_lock_remaining_sec": remaining,
            "proximity_lock_inhibited": ",".join(sorted(self._inhibitors)),
            "proximity_lock_last_result": self._last_result,
        }

    # ---- configuration ---------------------------------------------------

    def configure(self, enabled: bool, grace_sec: int) -> None:
        grace = clamp_grace(grace_sec)
        if not enabled:
            self._enabled = False
            self._grace = grace
            self._cancel_timer()
            self._absent_since = None
            self._set_state(STATE_DISABLED, force_emit=True)
            return
        was_enabled = self._enabled
        self._enabled = True
        self._grace = grace
        if not was_enabled:
            # Enabling never locks immediately; it needs the phone first.
            self._set_state(STATE_IDLE, force_emit=True)
        elif self._state == STATE_GRACE:
            self._arm_timer()
        self.refresh()

    # ---- observations ----------------------------------------------------

    def refresh(self) -> None:
        """Re-evaluate current bearer presence (call after bearer changes)."""
        if not self._enabled or self._inhibitors:
            return
        try:
            present = self._read_presence()
        except Exception:
            log.debug("proximity presence read failed", exc_info=True)
            return
        self._observe(present)

    def _observe(self, present: bool | None) -> None:
        if present is True:
            self._cancel_timer()
            self._absent_since = None
            if self._state != STATE_ARMED:
                self._set_state(STATE_ARMED)
            return
        if present is None:
            return
        if self._state == STATE_ARMED:
            self._absent_since = self._clock()
            self._set_state(STATE_GRACE)
            self._arm_timer()

    def inhibit(self, reason: str, active: bool = True) -> None:
        """Start or end one inhibitor; any change requires fresh presence."""
        if active:
            if reason in self._inhibitors:
                return
            self._inhibitors.add(reason)
            self._cancel_timer()
            self._absent_since = None
            if self._enabled:
                self._set_state(STATE_IDLE, force_emit=True)
            return
        if reason not in self._inhibitors:
            return
        self._inhibitors.discard(reason)
        if self._enabled:
            self._emit()
            self.refresh()

    def suspending(self) -> None:
        self.inhibit(INHIBIT_SLEEP, True)

    def resumed(self) -> None:
        """Call after the bearer state has been re-read following resume."""
        self.inhibit(INHIBIT_SLEEP, False)

    def reset(self) -> None:
        """Forget presence, e.g. when bluetoothd was replaced."""
        self._cancel_timer()
        self._absent_since = None
        if self._enabled and self._state != STATE_IDLE:
            self._set_state(STATE_IDLE)

    def stop(self) -> None:
        self._inhibitors.add(INHIBIT_STOPPED)
        self._cancel_timer()
        self._absent_since = None
        self._generation += 1

    # ---- timer and dispatch ---------------------------------------------

    def _arm_timer(self) -> None:
        self._cancel_timer()
        if self._absent_since is None:
            return
        remaining = self._absent_since + self._grace - self._clock()
        self._timer_id = self._schedule(
            max(1, math.ceil(remaining)), self._grace_elapsed
        )

    def _cancel_timer(self) -> None:
        if self._timer_id is None:
            return
        timer, self._timer_id = self._timer_id, None
        try:
            self._cancel(timer)
        except Exception:
            log.debug("could not remove proximity lock timer", exc_info=True)

    def _grace_elapsed(self) -> bool:
        self._timer_id = None
        if (
            not self._enabled
            or self._inhibitors
            or self._state != STATE_GRACE
            or self._absent_since is None
        ):
            return False
        # Re-read instead of trusting the last transition: a return may not
        # have produced a status callback yet.
        try:
            present = self._read_presence()
        except Exception:
            present = None
        if present is not False:
            if present is True:
                self._observe(True)
            else:
                # Unknown at the deadline: do not guess. Wait for a real
                # observation before arming again.
                self._absent_since = None
                self._set_state(STATE_IDLE)
            return False
        if self._clock() < self._absent_since + self._grace:
            self._arm_timer()
            return False
        self._dispatch()
        return False

    def _dispatch(self) -> None:
        self._absent_since = None
        self._set_state(STATE_LOCKED)
        if self._dispatching:
            return
        self._dispatching = True
        generation = self._generation
        log.info(
            "iPhone away for %ds; locking the desktop session", self._grace
        )

        def done(result: str) -> None:
            self._dispatching = False
            if generation != self._generation:
                return
            self._last_result = result
            if result == RESULT_FAILED:
                log.warning("proximity lock could not lock the desktop session")
            else:
                log.info("proximity lock locked the desktop via %s", result)
            self._emit()

        try:
            self._locker.lock(done)
        except Exception:
            log.exception("proximity lock dispatch failed")
            done(RESULT_FAILED)

    def _set_state(self, state: str, *, force_emit: bool = False) -> None:
        changed = state != self._state
        self._state = state
        if changed:
            log.debug("proximity lock state: %s", state)
        if changed or force_emit:
            self._emit()

    def _emit(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status()
            except Exception:
                log.debug("proximity lock status callback failed", exc_info=True)
