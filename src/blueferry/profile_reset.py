"""Heal profile connections bluetoothd keeps stuck, with one device reset.

Seen on real hardware on 2026-10-06 (BlueZ 5.87, oFono 2.18, iOS 27): the
Classic link stayed up while HFP could not be brought up again, and
``Device1.Disconnect`` followed by ``Device1.Connect`` cleared it. After a
reboot bluetoothd's own hfp plugin had won the race for the iPhone's RFCOMM
channel, so oFono's ``Modem.SetProperty(Powered, true)`` failed with
``org.ofono.Error.Timedout`` on every retry. Starting bluetoothd with
``-P hfp`` prevents the race; ``blueferry doctor`` checks it. The symptom
only exists with the opt-in phone calls, so nothing is ever reset otherwise.

Reasons are content-free tokens per profile, so a further symptom (for
example an A2DP ``ConnectProfile`` that keeps answering ``InProgress``) can
report into the same guards and rate limit.

This module only *decides*. It counts the symptoms, applies the guards and the
rate limit, and asks ``BearerSupervisor.reset_profiles`` to do the reset, so a
reset can never overlap a regular or manual reconnect. It never pages a phone
whose Classic link is down (a page to a vanishing phone coincided with
bluetoothd hanging in the kernel the same day), and never resets while
bluetoothd does not answer, during a call, or during the adapter recovery.

At most one automatic reset per ``AUTO_RESET_INTERVAL_SECONDS``; each further
automatic reset without a healthy profile in between doubles the wait, up to
``AUTO_RESET_BACKOFF_CAP_SECONDS``. Until then BlueFerry only suggests the
manual "Reconnect", which performs the same reset on request.

Status is content-free: reason tokens, a timestamp and booleans.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

# Content-free reason tokens; clients translate them.
REASON_HFP = "hfp_powered_timeout"
REASON_MANUAL = "manual"
REASONS = frozenset({REASON_HFP, REASON_MANUAL})

OFONO_TIMEDOUT = "org.ofono.Error.Timedout"
# Consecutive Powered=true timeouts with the Classic link up.
HFP_TIMEOUT_THRESHOLD = 2
AUTO_RESET_INTERVAL_SECONDS = 600
AUTO_RESET_BACKOFF_CAP_SECONDS = 3600

SETTINGS_KEY = "auto_profile_reset"
ENV_KEY = "BLUEFERRY_AUTO_PROFILE_RESET"


def auto_reset_enabled(path: Path | None = None) -> bool:
    """settings.json ``auto_profile_reset`` wins over the env/local.env value."""
    stored = SettingsStore(path or config.SETTINGS_JSON).read().get(SETTINGS_KEY)
    if isinstance(stored, bool):
        return stored
    return config.AUTO_PROFILE_RESET


def save_auto_reset(enabled: bool, path: Path | None = None) -> bool:
    """Save the opt-out in settings.json; it then wins over local.env."""
    if not isinstance(enabled, bool):
        raise ValueError("automatic profile reset must be a boolean")
    SettingsStore(path or config.SETTINGS_JSON).update(**{SETTINGS_KEY: enabled})
    return enabled


Reset = Callable[[], str]


class ProfileResetController:
    """Detect stuck HFP bring-ups and reset the device at most rarely."""

    def __init__(
        self,
        *,
        enabled: bool,
        reset: Reset,
        peer_connected: Callable[[], bool],
        bluez_blocked: Callable[[], bool],
        call_active: Callable[[], bool],
        recovering: Callable[[], bool],
        on_changed: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._enabled = bool(enabled)
        self._reset = reset
        self._peer_connected = peer_connected
        self._bluez_blocked = bluez_blocked
        self._call_active = call_active
        self._recovering = recovering
        self._on_changed = on_changed or (lambda: None)
        self._clock = clock
        self._wall_clock = wall_clock
        self._hfp_timeouts = 0
        # Stuck profile found but not (yet) healed automatically: the manual
        # Reconnect is offered for it.
        self._suggested = ""
        self._suppressed_logged = ""
        self._auto_streak = 0
        self._last_auto_mono: float | None = None
        self._next_auto_at = 0.0
        self._last_reason = ""
        self._last_at = 0
        self._last_auto = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Switch the automatic reset on or off at runtime (manual stays)."""
        if self._enabled == bool(enabled):
            return
        self._enabled = bool(enabled)
        # A suppression logged while switched off must be logged again.
        self._suppressed_logged = ""
        self._on_changed()

    @property
    def suggested(self) -> str:
        """Reason token of a stuck profile the user may want to reset."""
        return self._suggested if self._peer_connected() else ""

    def snapshot(self) -> dict[str, object]:
        return {
            "profile_reset_auto": self._enabled,
            "profile_reset_suggested": self._suggested,
            "profile_reset_last": self._last_reason,
            "profile_reset_last_at": self._last_at,
            "profile_reset_last_auto": self._last_auto,
        }

    def peer_lost(self) -> None:
        """The Classic link went down: earlier symptoms no longer apply."""
        self._hfp_timeouts = 0
        if self._suggested:
            self._suggested = ""
            self._on_changed()

    # ---- symptoms -----------------------------------------------------------

    def hfp_powered_failed(self, error_name: str) -> None:
        """oFono rejected Modem.Powered=true with ``error_name``."""
        if error_name != OFONO_TIMEDOUT or not self._peer_connected():
            # Another error, or a phone that is not there, breaks the streak:
            # only timeouts with a live link point at a held RFCOMM channel.
            self._hfp_timeouts = 0
            return
        self._hfp_timeouts += 1
        log.debug("oFono HFP power-up timed out (%d in a row)", self._hfp_timeouts)
        if self._hfp_timeouts >= HFP_TIMEOUT_THRESHOLD:
            self._stuck(REASON_HFP)

    def hfp_powered(self) -> None:
        self._hfp_timeouts = 0
        self._healthy(REASON_HFP)

    # ---- resets -------------------------------------------------------------

    def manual_reset(self) -> str:
        """The user asked for it ("Reconnect" while a profile is stuck).

        Skips the opt-out and the automatic rate limit, not the safety guards.
        """
        blocked = self._guard()
        if blocked:
            return blocked
        result = self._reset()
        if result == "started":
            self._record(REASON_MANUAL, auto=False)
            log.warning("resetting the iPhone's Bluetooth profiles on request")
        return result

    def _stuck(self, reason: str) -> bool:
        before = self.snapshot()
        self._suggested = reason
        started = self._auto_reset(reason)
        if self.snapshot() != before:
            self._on_changed()
        return started

    def _auto_reset(self, reason: str) -> bool:
        if not self._enabled:
            self._log_suppressed(reason, "automatic profile reset is switched off")
            return False
        blocked = self._guard()
        if blocked:
            log.debug("not resetting the iPhone's profiles (%s): %s", reason, blocked)
            return False
        if self._clock() < self._next_auto_at:
            self._log_suppressed(reason, "an automatic reset ran recently")
            return False
        result = self._reset()
        if result != "started":
            log.debug("profile reset not started (%s): %s", reason, result)
            return result == "in-progress"
        self._auto_streak += 1
        wait = min(
            AUTO_RESET_INTERVAL_SECONDS * 2 ** (self._auto_streak - 1),
            AUTO_RESET_BACKOFF_CAP_SECONDS,
        )
        self._last_auto_mono = self._clock()
        self._next_auto_at = self._last_auto_mono + wait
        self._record(reason, auto=True)
        log.warning(
            "iPhone %s looks stuck in bluetoothd (%s); resetting the device "
            "connection once (next automatic reset not before %d min)",
            "call audio (HFP)" if reason == REASON_HFP else reason,
            reason,
            wait // 60,
        )
        return True

    def _guard(self) -> str:
        if self._bluez_blocked():
            return "bluez-unresponsive"
        if self._recovering():
            return "recovery"
        if self._call_active():
            return "call-active"
        if not self._peer_connected():
            return "disconnected"
        return ""

    def _record(self, reason: str, *, auto: bool) -> None:
        self._last_reason = reason
        self._last_at = int(self._wall_clock())
        self._last_auto = auto
        self._suggested = ""
        self._suppressed_logged = ""
        self._hfp_timeouts = 0
        self._on_changed()

    def _healthy(self, reason: str) -> None:
        # A working profile ends the backoff; the minimum interval since the
        # last automatic reset still applies.
        self._auto_streak = 0
        if self._last_auto_mono is not None:
            self._next_auto_at = min(
                self._next_auto_at, self._last_auto_mono + AUTO_RESET_INTERVAL_SECONDS,
            )
        if self._suppressed_logged == reason:
            self._suppressed_logged = ""
        if self._suggested == reason:
            self._suggested = ""
            self._on_changed()

    def _log_suppressed(self, reason: str, why: str) -> None:
        if self._suppressed_logged == reason:
            return
        self._suppressed_logged = reason
        log.warning(
            "iPhone profile looks stuck in bluetoothd (%s); %s. Use "
            "'blueferry reconnect' or Reconnect in the app to reset it.",
            reason,
            why,
        )
