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
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gi.repository import GLib

from blueferry import config
from blueferry.ams.constants import COMMAND_NAMES, RemoteCommandID
from blueferry.ams.parsers import EntityUpdate
from blueferry.ams.state import NowPlaying
from blueferry.errors import InvalidArgumentsError, NotReadyError, OperationFailedError
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

# One track change arrives as up to four Track notifications plus Queue and
# Player updates. Coalesce them into a single client invalidation.
CHANGE_COALESCE_MS = 150
MAX_COMMAND_NAME_CHARS = 32

# Stable reasons reported as ``detail`` in GetNowPlaying and GetStatus.
DETAIL_DISABLED = "disabled"
DETAIL_REQUIRES_LE = "requires-notification-access-mode"
DETAIL_WAITING = "waiting-for-iphone"
# BlueZ does not report the LE bearer (no Bearer.LE1, for example BlueZ
# without the bearer API). AMS subscribes only on an observed LE link.
DETAIL_LE_UNKNOWN = "le-link-state-unknown"
DETAIL_READY = "ready"

Success = Callable[[], None]
Failure = Callable[[Exception], None]


class CommandWriter(Protocol):
    @property
    def available(self) -> bool: ...

    def send_command(
        self, command: RemoteCommandID, on_success: Success, on_failure: Failure,
    ) -> None: ...


class MediaControlSettings:
    """Persist the media-control opt-in in the owner-only settings document.

    ``BLUEFERRY_MEDIA_CONTROL_ENABLED`` provides the initial value. A choice
    saved through the D-Bus API (Qt settings, ``blueferry media enable``)
    takes precedence, like the proximity lock.
    """

    ENABLED_KEY = "media_control_enabled"

    def __init__(self, path: Path | None = None, *, default: bool | None = None) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        seeded = config.MEDIA_CONTROL_ENABLED if default is None else default
        stored = self._settings.read().get(self.ENABLED_KEY)  # {} when unreadable
        self._enabled = stored if isinstance(stored, bool) else bool(seeded)
        if (
            default is None
            and isinstance(stored, bool)
            and "BLUEFERRY_MEDIA_CONTROL_ENABLED" in os.environ
            and stored != bool(seeded)
        ):
            log.info(
                "BLUEFERRY_MEDIA_CONTROL_ENABLED is ignored because a media "
                "control preference was saved in settings.json (using %s)",
                stored,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("media control enabled must be a boolean")
        self._settings.update(**{self.ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled


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
        le_state: Callable[[], bool | None] | None = None,
    ) -> None:
        self.state = NowPlaying()
        self._clock = clock
        self._schedule = schedule
        self._cancel = cancel
        self._le_enabled = le_enabled
        self._le_state = le_state
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
            # present a stale track as current. The command list survives:
            # the AMS client reports an empty one when it is really gone.
            self.state.clear_playback()
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
        if self.available:
            return DETAIL_READY
        if self._le_state is not None and self._le_state() is None:
            return DETAIL_LE_UNKNOWN
        return DETAIL_WAITING

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
