"""Keep the ANCS solicitation aligned with end-to-end notification health."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from gi.repository import GLib

from blueferry import bluez_setup

log = logging.getLogger(__name__)

RECONCILE_SECONDS = 30
# Keep the post-pair/reconnect signal visible long enough for iOS to surface
# its MAP/PBAP permission toggles even if ANCS authorizes immediately.
MINIMUM_ON_SECONDS = 180

Register = Callable[[str], bool]
Unregister = Callable[[str], None]
IsRegistered = Callable[[], bool]
ForgetRegistration = Callable[[], None]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]


class SolicitationSupervisor:
    """Broadcast solicitation while ANCS needs an inbound LE connection.

    The advertisement is a reconnect primitive rather than a pairing-only
    side effect.  It remains on air while LE/GATT/authorization is incomplete,
    is released once ANCS is proven usable, and is recreated after BlueZ
    changes D-Bus owner.  A periodic reconciliation also repairs an
    advertisement that BlueZ released without another bearer transition.
    """

    def __init__(
        self,
        adapter: str,
        *,
        needed: bool = True,
        register: Register = bluez_setup.register_advert,
        unregister: Unregister = bluez_setup.unregister_advert,
        is_registered: IsRegistered = bluez_setup.advert_registered,
        forget_registration: ForgetRegistration = (
            bluez_setup.forget_advert_registration
        ),
        minimum_on_seconds: int = MINIMUM_ON_SECONDS,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
        clock: Clock = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self._needed = needed
        self._register = register
        self._unregister = unregister
        self._is_registered = is_registered
        self._forget_registration = forget_registration
        self._minimum_on_seconds = minimum_on_seconds
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._running = False
        self._timer_id: int | None = None
        self._hold_until = 0.0

    @property
    def needed(self) -> bool:
        return self._needed

    def active(self) -> bool:
        """Return whether inbound LE reconnection is currently primed."""
        return self._is_registered()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._needed:
            self._begin_hold()
        self._reconcile()
        self._timer_id = self._schedule(RECONCILE_SECONDS, self._tick)

    def set_needed(self, needed: bool) -> None:
        changed = needed != self._needed
        self._needed = needed
        if needed and changed:
            self._begin_hold()
        if self._running and (changed or needed != self._is_registered()):
            self._reconcile()

    def reset_after_bluez_restart(self) -> None:
        """Forget the old owner's registration and prime inbound LE again."""
        self._needed = True
        self._begin_hold()
        self._forget_registration()
        if self._running:
            self._reconcile()

    def stop(self) -> None:
        self._running = False
        if self._timer_id is not None:
            try:
                self._cancel(self._timer_id)
            except Exception:
                log.debug("could not remove solicitation health timer", exc_info=True)
            self._timer_id = None
        if self._is_registered():
            self._unregister(self.adapter)

    def _tick(self) -> bool:
        if not self._running:
            return False
        self._reconcile()
        return True

    def _reconcile(self) -> None:
        registered = self._is_registered()
        if self._needed and not registered:
            if not self._register(self.adapter):
                log.warning(
                    "ANCS solicitation is unavailable; retrying in %ds",
                    RECONCILE_SECONDS,
                )
        elif (
            not self._needed
            and registered
            and self._clock() >= self._hold_until
        ):
            self._unregister(self.adapter)

    def _begin_hold(self) -> None:
        self._hold_until = max(
            self._hold_until,
            self._clock() + self._minimum_on_seconds,
        )
