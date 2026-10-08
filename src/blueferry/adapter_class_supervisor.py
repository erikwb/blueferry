"""Keep the Bluetooth adapter identity required by iOS MAP/PBAP."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from gi.repository import GLib

from blueferry import bluez_setup, service_manager

log = logging.getLogger(__name__)

RECONCILE_SECONDS = 60
# Until a repair lands, iOS refuses the Classic MAP/PBAP path, so a failure
# that may clear on its own (polkit agent missing, the systemd unit failing,
# btmgmt erroring) is retried every minute, as before. Only the sudo path
# backs off, because each attempt there is an authentication event in the
# system log:
# - a recognised refusal (CodAuthorizationRefused) only changes when an
#   administrator edits sudoers, so it starts at 15 minutes and doubles up
#   to 6 hours;
# - any other failure without systemd (unknown sudo wording, btmgmt) starts
#   at one minute and is capped at a few minutes, so a cause that clears is
#   still repaired soon.
# A matching class, a successful repair or a BlueZ restart reset the backoff.
#
# Right after bluetoothd restarts, the controller can still be busy: btmgmt
# then fails with "Set Dev Class failed, status 0x0a (Busy)" (CodAdapterBusy)
# while the class reads 0x000000. That clears within seconds, so it is not a
# failure: the supervisor retries once after BUSY_RETRY_SECONDS through a
# one-shot timer, without backoff, up to MAX_BUSY_RETRIES times in a row.
# Only then does the attempt count as a failure and back off as above. A
# matching class, a successful repair or a BlueZ restart reset the count.
FAILED_REPAIR_BACKOFF_SECONDS = 60
MAX_FAILED_REPAIR_BACKOFF_SECONDS = 5 * 60
SYSTEMD_RETRY_GUARD_SECONDS = RECONCILE_SECONDS // 2
BUSY_RETRY_SECONDS = 3
MAX_BUSY_RETRIES = 5
REFUSED_REPAIR_BACKOFF_SECONDS = 15 * 60
MAX_REPAIR_BACKOFF_SECONDS = 6 * 60 * 60

ReadClass = Callable[[str], int | None]
Matches = Callable[[int | None], bool]
Repair = Callable[[str], bool]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]
UsesSudo = Callable[[], bool]


def _repair_with_packaged_helper(adapter: str) -> bool:
    return bluez_setup.set_cod(adapter=adapter, authorize=True)


def _repairs_through_sudo() -> bool:
    """Return whether repairs go through sudo (no systemd/polkit)."""
    return service_manager.init_system() != service_manager.SYSTEMD


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
        uses_sudo: UsesSudo = _repairs_through_sudo,
    ) -> None:
        self.adapter = adapter
        self._read_class = read_class
        self._matches = matches
        self._repair = repair
        self._schedule = schedule
        self._cancel = cancel
        self._running = False
        self._clock = clock
        self._uses_sudo = uses_sudo
        self._timer_id: int | None = None
        # After a failed sudo repair, no new attempt before ``_retry_at``. A
        # missing sudoers rule is then not retried (and logged by sudo)
        # every minute, yet a rule the admin adds later still takes effect
        # without a Bluetooth restart.
        self._backoff = 0.0
        self._retry_at: float | None = None
        # Quick one-shot retries while btmgmt reports the adapter busy.
        self._busy_retries = 0
        self._busy_timer_id: int | None = None

    def start(self) -> None:
        if self._running:
            self.poke()
            return
        self._reset_backoff()
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
        self._busy_retries = 0
        busy_timer_id, self._busy_timer_id = self._busy_timer_id, None
        if busy_timer_id is not None:
            try:
                self._cancel(busy_timer_id)
            except Exception:
                log.debug("could not remove adapter-busy retry timer", exc_info=True)
        if self._timer_id is None:
            return
        try:
            self._cancel(self._timer_id)
        except Exception:
            log.debug("could not remove adapter-class health timer", exc_info=True)
        self._timer_id = None

    def recheck(self) -> None:
        """Consume a completed asynchronous class read without refunding backoff."""
        if self._running:
            self._reconcile()

    def _tick(self) -> bool:
        if not self._running:
            return False
        self._reconcile()
        return True

    def _busy_retry(self) -> bool:
        self._busy_timer_id = None
        if self._running:
            self._reconcile()
        return False

    def _retry_busy_soon(self) -> bool:
        """Plan a quick retry after Busy; False once the budget is spent."""
        if self._busy_retries >= MAX_BUSY_RETRIES:
            return False
        self._busy_retries += 1
        if self._busy_timer_id is None:
            self._busy_timer_id = self._schedule(
                BUSY_RETRY_SECONDS, self._busy_retry,
            )
        log.info("adapter busy; retrying in %d s", BUSY_RETRY_SECONDS)
        return True

    def _reset_backoff(self) -> None:
        self._backoff = 0.0
        self._retry_at = None
        self._busy_retries = 0

    def _defer_retry(self, initial: float, cap: float) -> float:
        self._backoff = min(max(self._backoff * 2, initial), cap)
        self._retry_at = self._clock() + self._backoff
        return self._backoff

    def _defer_failed_retry(self) -> float:
        """Defer after a failure that may clear without an administrator."""
        try:
            uses_sudo = self._uses_sudo()
        except Exception:
            log.debug("could not detect the service manager", exc_info=True)
            uses_sudo = True
        if not uses_sudo:
            # systemd/polkit: retry on the next periodic tick, as without
            # backoff. The short guard only keeps a completed asynchronous
            # class read (recheck) from repeating the repair within the
            # same minute.
            self._backoff = 0.0
            self._retry_at = self._clock() + SYSTEMD_RETRY_GUARD_SECONDS
            return RECONCILE_SECONDS
        return self._defer_retry(
            FAILED_REPAIR_BACKOFF_SECONDS, MAX_FAILED_REPAIR_BACKOFF_SECONDS,
        )

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
        # A quick retry after Busy repeats the same repair; warn only once.
        log.log(
            logging.DEBUG if self._busy_retries else logging.WARNING,
            "adapter Class-of-Device drifted to 0x%06x; restoring A/V Hands-Free",
            cod,
        )
        try:
            repaired = self._repair(self.adapter)
        except bluez_setup.CodAdapterBusy:
            if self._retry_busy_soon():
                return
            delay = self._defer_failed_retry()
            log.warning(
                "adapter still busy after %d quick retries; retrying in %d seconds",
                MAX_BUSY_RETRIES, delay,
            )
            return
        except bluez_setup.CodAuthorizationRefused as error:
            delay = self._defer_retry(
                REFUSED_REPAIR_BACKOFF_SECONDS, MAX_REPAIR_BACKOFF_SECONDS,
            )
            log.warning(
                "could not restore adapter Class-of-Device: %s "
                "Retrying in %d minutes or after the next Bluetooth restart.",
                error, delay // 60,
            )
            return
        except Exception:
            delay = self._defer_failed_retry()
            log.warning(
                "could not restore adapter Class-of-Device; retrying in %d seconds",
                delay, exc_info=True,
            )
            return
        if repaired:
            self._reset_backoff()
            log.info("adapter Class-of-Device restored through packaged helper")
        else:
            delay = self._defer_failed_retry()
            log.warning(
                "packaged adapter-class helper did not repair the adapter; "
                "retrying in %d seconds",
                delay,
            )
