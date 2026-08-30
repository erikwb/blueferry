"""Keep the Bluetooth adapter identity required by iOS MAP/PBAP."""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import GLib

from blueferry import bluez_setup

log = logging.getLogger(__name__)

RECONCILE_SECONDS = 60

ReadClass = Callable[[str], int | None]
Matches = Callable[[int | None], bool]
Repair = Callable[[str], bool]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


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
    ) -> None:
        self.adapter = adapter
        self._read_class = read_class
        self._matches = matches
        self._repair = repair
        self._schedule = schedule
        self._cancel = cancel
        self._running = False
        self._timer_id: int | None = None

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
            return
        log.warning(
            "adapter Class-of-Device drifted to 0x%06x; restoring A/V Hands-Free",
            cod,
        )
        try:
            repaired = self._repair(self.adapter)
        except Exception:
            log.warning("could not restore adapter Class-of-Device", exc_info=True)
            return
        if repaired:
            log.info("adapter Class-of-Device restored through packaged helper")
        else:
            log.warning("packaged adapter-class helper did not repair the adapter")
