"""Backend-owned now-playing projection built from AMS entity updates."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from blueferry.ams.constants import (
    COMMAND_IDS,
    EntityID,
    PlaybackState,
    PlayerAttributeID,
    QueueAttributeID,
    RemoteCommandID,
    TrackAttributeID,
)
from blueferry.ams.parsers import (
    EntityUpdate,
    clean_text,
    parse_count,
    parse_duration,
    parse_mode,
    parse_playback_info,
    parse_volume,
)

log = logging.getLogger(__name__)

PLAYBACK_STATE_NAMES = {
    PlaybackState.Paused: "paused",
    PlaybackState.Playing: "playing",
    PlaybackState.Rewinding: "rewinding",
    PlaybackState.FastForwarding: "fast-forwarding",
}
MODE_NAMES = {0: "off", 1: "one", 2: "all"}


@dataclass(slots=True)
class NowPlaying:
    """Latest values reported by the iPhone's Media Source.

    ``elapsed`` is a snapshot taken at ``elapsed_at`` (monotonic seconds);
    iOS does not stream the playback position, so readers extrapolate it with
    the reported rate.
    """

    player_name: str | None = None
    playback_state: PlaybackState | None = None
    playback_rate: float | None = None
    elapsed: float | None = None
    elapsed_at: float | None = None
    volume: float | None = None
    queue_index: int | None = None
    queue_count: int | None = None
    shuffle: int | None = None
    repeat: int | None = None
    artist: str | None = None
    album: str | None = None
    title: str | None = None
    duration: float | None = None
    supported_commands: frozenset[RemoteCommandID] = field(default_factory=frozenset)

    def reset(self) -> None:
        self.clear_playback()
        self.supported_commands = frozenset()

    def clear_playback(self) -> None:
        """Forget what is playing but keep the phone's command list.

        iOS sends its supported commands once per CCC enable, and BlueZ
        re-enables a surviving registration by itself at LE link-up, so the
        list is not necessarily sent again after a reconnect.
        """
        for name in self.__slots__:
            if name != "supported_commands":
                setattr(self, name, None)

    def set_supported_commands(self, commands: frozenset[RemoteCommandID]) -> bool:
        if commands == self.supported_commands:
            return False
        self.supported_commands = commands
        return True

    def apply(self, update: EntityUpdate, now: float) -> bool:
        """Apply one update; return whether the visible state changed.

        Malformed values for a known attribute are ignored and logged without
        their content. Unknown entities and attributes are future AMS fields.
        """
        before = self._fingerprint()
        try:
            self._apply(update, now)
        except ValueError:
            log.warning(
                "ignoring malformed AMS value (entity=%d attribute=%d, %d chars)",
                update.entity, update.attribute, len(update.value),
            )
            return False
        return self._fingerprint() != before

    def _apply(self, update: EntityUpdate, now: float) -> None:
        value = update.value
        if update.entity == EntityID.Player:
            if update.attribute == PlayerAttributeID.Name:
                self.player_name = clean_text(value) or None
            elif update.attribute == PlayerAttributeID.PlaybackInfo:
                info = parse_playback_info(value)
                self.playback_state = info.state
                self.playback_rate = info.rate
                self.elapsed = info.elapsed
                self.elapsed_at = now if info.elapsed is not None else None
            elif update.attribute == PlayerAttributeID.Volume:
                self.volume = parse_volume(value)
        elif update.entity == EntityID.Queue:
            if update.attribute == QueueAttributeID.Index:
                self.queue_index = parse_count(value)
            elif update.attribute == QueueAttributeID.Count:
                self.queue_count = parse_count(value)
            elif update.attribute == QueueAttributeID.ShuffleMode:
                self.shuffle = parse_mode(value)
            elif update.attribute == QueueAttributeID.RepeatMode:
                self.repeat = parse_mode(value)
        elif update.entity == EntityID.Track:
            if update.attribute == TrackAttributeID.Artist:
                self.artist = clean_text(value) or None
            elif update.attribute == TrackAttributeID.Album:
                self.album = clean_text(value) or None
            elif update.attribute == TrackAttributeID.Title:
                self.title = clean_text(value) or None
            elif update.attribute == TrackAttributeID.Duration:
                self.duration = parse_duration(value)

    def _fingerprint(self) -> tuple:
        return (
            self.player_name, self.playback_state, self.playback_rate,
            self.elapsed, self.volume, self.queue_index, self.queue_count,
            self.shuffle, self.repeat, self.artist, self.album, self.title,
            self.duration, self.supported_commands,
        )

    @property
    def playing(self) -> bool:
        return self.playback_state in (
            PlaybackState.Playing,
            PlaybackState.Rewinding,
            PlaybackState.FastForwarding,
        )

    def position(self, now: float) -> float | None:
        """Extrapolated playback position in seconds, clamped to the track."""
        if self.elapsed is None:
            return None
        position = self.elapsed
        if self.playing and self.elapsed_at is not None and self.playback_rate:
            position += self.playback_rate * max(0.0, now - self.elapsed_at)
        if self.duration is not None and self.duration > 0:
            position = min(position, self.duration)
        return max(0.0, position)

    def snapshot(self, now: float) -> dict[str, object]:
        state = (
            PLAYBACK_STATE_NAMES.get(self.playback_state)
            if self.playback_state is not None else None
        )
        return {
            "player": {
                "name": self.player_name,
                "state": state,
                "rate": self.playback_rate,
                "volume": self.volume,
            },
            "queue": {
                "index": self.queue_index,
                "count": self.queue_count,
                "shuffle": MODE_NAMES.get(self.shuffle) if self.shuffle is not None else None,
                "repeat": MODE_NAMES.get(self.repeat) if self.repeat is not None else None,
            },
            "track": {
                "artist": self.artist,
                "album": self.album,
                "title": self.title,
                "duration": self.duration,
                "elapsed": self.position(now),
            },
            "supported_commands": sorted(
                COMMAND_IDS[command] for command in self.supported_commands
            ),
        }
