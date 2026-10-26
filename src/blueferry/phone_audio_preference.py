"""Apply a user's media route once per connection, after pairing has finished."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from blueferry.audio_activity import audio_activity
from blueferry.errors import InvalidArgumentsError, NotReadyError
from blueferry.phone_audio_route import PhoneAudioRoute
from blueferry.phone_audio_settings import CHOICES, PhoneAudioSettings

log = logging.getLogger(__name__)
Configure = Callable[[bool, Callable[[str | Exception], None]], None]


@dataclass
class _Request:
    choice: str
    success: Callable[[str], None]
    failure: Callable[[Exception], None]
    deadline: float


class PhoneAudioPreference:
    def __init__(
        self, route: PhoneAudioRoute, settings: PhoneAudioSettings, *,
        configure: Configure, connected: Callable[[], bool | None],
        safe: Callable[[], bool], automatic_ready: Callable[[], bool],
        schedule: Callable[[int, Callable[[], bool]], int], cancel: Callable[[int], object],
        on_changed: Callable[[], None], clock: Callable[[], float] = time.monotonic,
        guard: Callable[[], AbstractContextManager] | None = None,
        policy_present: bool = False,
    ) -> None:
        self.route = route
        self.settings = settings
        self._configure = configure
        self._connected = connected
        self._safe = safe
        self._automatic_ready = automatic_ready
        self._schedule = schedule
        self._cancel = cancel
        self._on_changed = on_changed
        self._clock = clock
        self._guard = guard or (lambda: audio_activity(blocking=False))
        self._running = False
        self._timer: int | None = None
        self._request: _Request | None = None
        self._configuring = False
        self._policy_applied: bool | None = (
            False if settings.choice == "auto" and not policy_present else None
        )
        self._policy_state = "desktop"
        self._policy_failed = False
        self._attempted = False
        self._last_connected: bool | None = None
        self._generation = 0
        self._operation: AbstractContextManager | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "phone_audio_preference": self.settings.choice,
            "phone_audio_autoconnect_policy": self._policy_state,
            "phone_audio_pending": (
                self._request.choice
                if self._request is not None and self._request.choice != "auto"
                else self.route.snapshot().get("phone_audio_pending", "")
            ),
        }

    def start(self) -> None:
        self._running = True
        self._last_connected = self._connected()
        self.poke()

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
        self._release()
        self._fail(NotReadyError("the backend stopped during audio routing"))

    def connection_changed(self) -> None:
        connected = self._connected()
        if connected is False or (connected is True and self._last_connected is False):
            self._attempted = False
        if connected is not None:
            self._last_connected = connected
        self.poke()

    def bluez_restarted(self) -> None:
        self._attempted = False
        self._last_connected = None
        self.poke()

    def poke(self) -> None:
        if not self._running or self._timer is not None or self._configuring:
            return
        if (
            self._request is None and self.settings.choice == "auto"
            and self._policy_applied is False
        ):
            return
        self._timer = self._schedule(1, self._tick)

    def set_route(self, choice: str, success, failure) -> None:
        if choice not in CHOICES:
            raise InvalidArgumentsError("route must be 'pc', 'phone' or 'auto'")
        if not self._running or self._request is not None or self.route.busy:
            raise NotReadyError("an audio route change is already in progress")
        if choice != "auto" and self.route.snapshot()["phone_audio_route"] == "unavailable":
            raise NotReadyError("the iPhone's audio route is unavailable")
        self._request = _Request(choice, success, failure, self._clock() + 25)
        self._policy_failed = False
        self._on_changed()
        self.poke()

    def _fail(self, error: Exception) -> None:
        request, self._request = self._request, None
        if request is not None:
            request.failure(error)
            self._on_changed()

    def _save(self, choice: str) -> None:
        request, self._request = self._request, None
        try:
            self.settings.set(choice)
        except (OSError, ValueError) as error:
            if request is not None:
                request.failure(NotReadyError(f"could not save the audio preference: {error}"))
        else:
            if request is not None:
                request.success(choice)
        self._on_changed()
        self.poke()

    def _configured(self, enabled: bool, generation: int, result: str | Exception) -> None:
        if generation != self._generation or not self._running:
            return
        self._configuring = False
        if isinstance(result, BlockingIOError):
            # Pairing owns the operation boundary; retry after it releases it.
            self.poke()
            return
        if isinstance(result, Exception):
            self._policy_failed = True
            self._policy_state = "failed"
            log.warning("could not apply the phone audio rule: %s", result)
            self._fail(NotReadyError("could not apply the phone audio rule; see the backend log"))
        else:
            self._policy_applied = enabled
            self._policy_state = result
            self._attempted = False
            # The reload has completed, but endpoints register afterwards.
            self.route.probe()
        self._on_changed()
        self.poke()

    def _tick(self) -> bool:
        self._timer = None
        if not self._running or self._operation is not None:
            return False
        request = self._request
        if request is not None and self._clock() >= request.deadline:
            self._fail(NotReadyError("Bluetooth is still busy; retry the audio change"))
            self.poke()
            return False
        choice = request.choice if request is not None else self.settings.choice
        enabled = choice != "auto"
        if self._policy_failed and request is None:
            return False
        if not self._safe():
            self.poke()
            return False
        if request is None and not self._automatic_ready():
            # A reload also tears down audio endpoints. Wait for setup proof
            # before doing either policy work or an automatic profile change.
            self.poke()
            return False
        if self._policy_applied != enabled:
            self._configuring = True
            generation = self._generation
            try:
                self._configure(
                    enabled, lambda result: self._configured(enabled, generation, result),
                )
            except Exception as error:
                self._configured(enabled, generation, error)
            return False
        if choice == "auto":
            if request is not None:
                self._save(choice)
            return False
        if request is None and self._attempted:
            return False
        current = self.route.snapshot()["phone_audio_route"]
        if current == "unavailable":
            self.poke()
            return False
        if request is not None and current == choice:
            self._attempted = True
            self._save(choice)
            return False
        if request is None and current == choice:
            # Wait for the first unwanted incoming stream when preferring the
            # phone. A PC route already present needs no outgoing request.
            if choice == "pc":
                self._attempted = True
            return False
        if choice == "pc" and not self.route.sink_available:
            self.route.probe()
            self.poke()
            return False
        operation = self._guard()
        try:
            operation.__enter__()
        except BlockingIOError:
            self.poke()
            return False
        except OSError as error:
            self._fail(NotReadyError(f"could not coordinate the audio change: {error}"))
            self._attempted = True
            return False
        self._operation = operation
        self._attempted = True
        generation = self._generation

        def success(selected: str) -> None:
            if generation != self._generation:
                return
            self._release()
            if request is not None and self._request is request:
                self._save(selected)
            self._on_changed()

        def failure(error: Exception) -> None:
            if generation != self._generation:
                return
            self._release()
            if request is not None and self._request is request:
                self._fail(error)
            else:
                log.info("could not restore the saved audio route: %s", error)
            self._on_changed()
            # One automatic attempt per Classic connection, never a battle
            # with iOS or another audio manager reconnecting the stream.

        try:
            self.route.set_route(choice, success, failure)
        except Exception as error:
            failure(error)
        return False

    def _release(self) -> None:
        operation, self._operation = self._operation, None
        if operation is not None:
            operation.__exit__(None, None, None)
