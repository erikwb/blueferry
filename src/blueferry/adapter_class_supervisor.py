"""Keep the Bluetooth adapter identity required by iOS MAP/PBAP."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from gi.repository import GLib

from blueferry import bluez_setup

log = logging.getLogger(__name__)

RECONCILE_SECONDS = 60
# A failed repair is retried with exponential backoff instead of every
# minute: each attempt can be an authentication event in the system log
# (sudo, polkit). A refusal starts higher because only an administrator can
# change its outcome. Matching class, success, or a BlueZ restart reset it.
FAILED_REPAIR_BACKOFF_SECONDS = 60
REFUSED_REPAIR_BACKOFF_SECONDS = 15 * 60
MAX_REPAIR_BACKOFF_SECONDS = 6 * 60 * 60

ReadClass = Callable[[str], int | None]
Matches = Callable[[int | None], bool]
Repair = Callable[[str], bool]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]


def _repair_with_packaged_helper(adapter: str) -> bool:
    return bluez_setup.set_cod(adapter=adapter, authorize=True)


class AdapterClassSupervisor:
    """Repair Class-of-Device drift through the constrained system helper.

    ``btmgmt class`` is volatile across controller and bluetoothd resets. A
    stale generic-computer class leaves an existing LE/ANCS bond usable while
    iOS refuses the Classic MAP/PBAP accessory path, so startup-only pairing
    configuration is not sufficient.
    """

    def __init__(
        self,
        adapter: str,
        *,
        read_class: ReadClass = bluez_setup.current_cod,
        matches: Matches = bluez_setup.desired_cod_matches,
        repair: Repair = _repair_with_packaged_helper,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
        clock: Clock = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self._read_class = read_class
        self._matches = matches
        self._repair = repair
        self._schedule = schedule
        self._cancel = cancel
        self._running = False
        self._clock = clock
        self._timer_id: int | None = None
        # After a failed repair, no new attempt before ``_retry_at``. A
        # missing sudoers rule or polkit agent is then not retried (and
        # logged by sudo or polkit) every minute, yet a rule the admin adds
        # later still takes effect without a Bluetooth restart.
        self._backoff = 0.0
        self._retry_at: float | None = None

    def start(self) -> None:
        if self._running:
            self.poke()
            return
        self._running = True
        self._reconcile()
        self._timer_id = self._schedule(RECONCILE_SECONDS, self._tick)

    def poke(self) -> None:
        """Recheck immediately, notably after bluetoothd changes owner."""
        if self._running:
            self._reset_backoff()
            self._reconcile()

    def stop(self) -> None:
        self._running = False
        if self._timer_id is None:
            return
        try:
            self._cancel(self._timer_id)
        except Exception:
            log.debug("could not remove adapter-class health timer", exc_info=True)
        self._timer_id = None

    def _tick(self) -> bool:
        if not self._running:
            return False
        self._reconcile()
        return True

    def _reset_backoff(self) -> None:
        self._backoff = 0.0
        self._retry_at = None

    def _defer_retry(self, initial: float) -> float:
        self._backoff = min(
            max(self._backoff * 2, initial), MAX_REPAIR_BACKOFF_SECONDS,
        )
        self._retry_at = self._clock() + self._backoff
        return self._backoff

    def _reconcile(self) -> None:
        try:
            cod = self._read_class(self.adapter)
        except Exception:
            log.debug("could not inspect adapter Class-of-Device", exc_info=True)
            return
        if cod is None:
            log.debug("adapter Class-of-Device is temporarily unavailable")
            return
        if self._matches(cod):
            self._reset_backoff()
            return
        if self._retry_at is not None and self._clock() < self._retry_at:
            return
        log.warning(
            "adapter Class-of-Device drifted to 0x%06x; restoring A/V Hands-Free",
            cod,
        )
        try:
            repaired = self._repair(self.adapter)
        except bluez_setup.CodAuthorizationRefused as error:
            delay = self._defer_retry(REFUSED_REPAIR_BACKOFF_SECONDS)
            log.warning(
                "could not restore adapter Class-of-Device: %s "
                "Retrying in %d minutes or after the next Bluetooth restart.",
                error, delay // 60,
            )
            return
        except Exception:
            delay = self._defer_retry(FAILED_REPAIR_BACKOFF_SECONDS)
            log.warning(
                "could not restore adapter Class-of-Device; retrying in %d seconds",
                delay, exc_info=True,
            )
            return
        if repaired:
            self._reset_backoff()
            log.info("adapter Class-of-Device restored through packaged helper")
        else:
            delay = self._defer_retry(FAILED_REPAIR_BACKOFF_SECONDS)
            log.warning(
                "packaged adapter-class helper did not repair the adapter; "
                "retrying in %d seconds",
                delay,
            )
