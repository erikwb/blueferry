"""Opt-in iPhone now-playing state and media commands (Apple Media Service).

``MediaController`` owns the now-playing projection and command policy. It is
independent of D-Bus and BlueZ: the daemon feeds it AMS updates and attaches
the :class:`~blueferry.ams.client.AmsClient` that writes commands. Listeners
(such as the content-free ``NowPlayingChanged`` signal)
receive one coalesced invalidation per burst of updates, because iOS reports a
track change as several separate attribute notifications.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from gi.repository import GLib

from blueferry.ams.constants import COMMAND_NAMES, RemoteCommandID
from blueferry.ams.parsers import EntityUpdate
from blueferry.ams.state import NowPlaying
from blueferry.errors import InvalidArgumentsError, NotReadyError, OperationFailedError

log = logging.getLogger(__name__)

# One track change arrives as up to four Track notifications plus Queue and
# Player updates. Coalesce them into a single client invalidation.
CHANGE_COALESCE_MS = 150
MAX_COMMAND_NAME_CHARS = 32

# Stable reasons reported as ``detail`` in GetNowPlaying and GetStatus.
DETAIL_DISABLED = "disabled"
DETAIL_REQUIRES_LE = "requires-notification-access-mode"
DETAIL_WAITING = "waiting-for-iphone"
DETAIL_READY = "ready"

Success = Callable[[], None]
Failure = Callable[[Exception], None]


class CommandWriter(Protocol):
    @property
    def available(self) -> bool: ...

    def send_command(
        self, command: RemoteCommandID, on_success: Success, on_failure: Failure,
    ) -> None: ...


def disabled_snapshot(detail: str = DETAIL_DISABLED) -> dict[str, object]:
    return {"enabled": False, "available": False, "detail": detail}


class MediaController:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add,
        cancel: Callable[[int], object] = GLib.source_remove,
        le_enabled: bool = True,
    ) -> None:
        self.state = NowPlaying()
        self._clock = clock
        self._schedule = schedule
        self._cancel = cancel
        self._le_enabled = le_enabled
        self._writer: CommandWriter | None = None
        self._listeners: list[Callable[[], None]] = []
        self._pending_change: int | None = None

    # ---- wiring ---------------------------------------------------------

    def attach(self, writer: CommandWriter | None) -> None:
        self._writer = writer

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def close(self) -> None:
        if self._pending_change is not None:
            try:
                self._cancel(self._pending_change)
            except Exception:
                log.debug("could not remove media change timer", exc_info=True)
            self._pending_change = None
        self._listeners.clear()
        self._writer = None

    # ---- AMS input ------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._writer is not None and self._writer.available)

    def handle_update(self, update: EntityUpdate) -> None:
        if self.state.apply(update, self._clock()):
            self._changed()

    def handle_supported_commands(self, commands: frozenset[RemoteCommandID]) -> None:
        if self.state.set_supported_commands(commands):
            self._changed()

    def handle_availability(self, available: bool) -> None:
        if not available:
            # Nothing is known about playback once the link is gone; never
            # present a stale track as current.
            self.state.reset()
        log.info("iPhone media control %s", "available" if available else "unavailable")
        self._changed()

    def _changed(self) -> None:
        if self._pending_change is not None:
            return
        self._pending_change = self._schedule(CHANGE_COALESCE_MS, self._flush)

    def _flush(self) -> bool:
        self._pending_change = None
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("media change listener failed")
        return False

    # ---- application operations ----------------------------------------

    def detail(self) -> str:
        if not self._le_enabled:
            return DETAIL_REQUIRES_LE
        return DETAIL_READY if self.available else DETAIL_WAITING

    def snapshot(self) -> dict[str, object]:
        available = self.available
        result: dict[str, object] = {
            "enabled": True,
            "available": available,
            "detail": self.detail(),
        }
        if available:
            result.update(self.state.snapshot(self._clock()))
        return result

    def resolve_command(self, name: str) -> RemoteCommandID:
        """Validate a public command name against the phone's advertised set."""
        if not isinstance(name, str) or len(name) > MAX_COMMAND_NAME_CHARS:
            raise InvalidArgumentsError("invalid media command")
        command = COMMAND_NAMES.get(name.strip().casefold())
        if command is None:
            raise InvalidArgumentsError(
                "unknown media command; expected one of: "
                + ", ".join(sorted(COMMAND_NAMES))
            )
        if not self.available:
            raise NotReadyError("iPhone media control is not connected")
        supported = self.state.supported_commands
        if command in supported:
            return command
        # Many players advertise only one of toggle or play/pause.
        if command == RemoteCommandID.TogglePlayPause:
            fallback = RemoteCommandID.Pause if self.state.playing else RemoteCommandID.Play
            if fallback in supported:
                return fallback
        elif command in (RemoteCommandID.Play, RemoteCommandID.Pause):
            if RemoteCommandID.TogglePlayPause in supported and (
                (command == RemoteCommandID.Play) != self.state.playing
            ):
                return RemoteCommandID.TogglePlayPause
        raise NotReadyError("the iPhone does not currently offer this media command")

    def send_command(self, name: str, on_success: Success, on_failure: Failure) -> None:
        command = self.resolve_command(name)
        writer = self._writer
        if writer is None:
            raise NotReadyError("iPhone media control is not connected")
        log.info("sending iPhone media command %s", command.name)
        writer.send_command(
            command,
            on_success,
            lambda error: on_failure(OperationFailedError("MediaCommand", error)),
        )
