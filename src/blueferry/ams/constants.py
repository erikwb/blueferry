"""Apple Media Service wire identifiers.

Values follow Apple's "Apple Media Service Reference" (AMS). The iPhone is the
Media Source (GATT server); BlueFerry is a Media Remote (GATT client) on the
same LE bond that already carries ANCS.
"""
from __future__ import annotations

from enum import IntEnum

AMS_SERVICE_UUID = "89d3502b-0f36-433a-8ef4-c502ad55f8dc"
REMOTE_COMMAND_CHAR = "9b3c81d8-57b1-4a8a-b8df-0e56f7ca51c2"
ENTITY_UPDATE_CHAR = "2f7cabce-808d-411f-9a0c-bb92ba96c102"
ENTITY_ATTRIBUTE_CHAR = "c6b2f38c-23ab-46d8-a6ab-a3a870bbd5d7"
AMS_CHAR_UUIDS = frozenset({
    REMOTE_COMMAND_CHAR,
    ENTITY_UPDATE_CHAR,
    ENTITY_ATTRIBUTE_CHAR,
})


class RemoteCommandID(IntEnum):
    Play = 0
    Pause = 1
    TogglePlayPause = 2
    NextTrack = 3
    PreviousTrack = 4
    VolumeUp = 5
    VolumeDown = 6
    AdvanceRepeatMode = 7
    AdvanceShuffleMode = 8
    SkipForward = 9
    SkipBackward = 10
    LikeTrack = 11
    DislikeTrack = 12
    BookmarkTrack = 13


class EntityID(IntEnum):
    Player = 0
    Queue = 1
    Track = 2


class PlayerAttributeID(IntEnum):
    Name = 0
    PlaybackInfo = 1
    Volume = 2


class QueueAttributeID(IntEnum):
    Index = 0
    Count = 1
    ShuffleMode = 2
    RepeatMode = 3


class TrackAttributeID(IntEnum):
    Artist = 0
    Album = 1
    Title = 2
    Duration = 3


class EntityUpdateFlag:
    Truncated = 1 << 0


class PlaybackState(IntEnum):
    Paused = 0
    Playing = 1
    Rewinding = 2
    FastForwarding = 3


class ShuffleMode(IntEnum):
    Off = 0
    One = 1
    All = 2


class RepeatMode(IntEnum):
    Off = 0
    One = 1
    All = 2


# ATT application error codes defined by AMS. BlueZ surfaces them only in
# the D-Bus error message text; the client extracts and logs the name.
AMS_ERROR_INVALID_STATE = 0xA0
AMS_ERROR_INVALID_COMMAND = 0xA1
AMS_ERROR_ABSENT_ATTRIBUTE = 0xA2
AMS_ERROR_NAMES: dict[int, str] = {
    AMS_ERROR_INVALID_STATE: "InvalidState",
    AMS_ERROR_INVALID_COMMAND: "InvalidCommand",
    AMS_ERROR_ABSENT_ATTRIBUTE: "AbsentAttribute",
}

# Attributes registered per entity. Every attribute AMS defines is used.
ENTITY_ATTRIBUTES: dict[EntityID, tuple[int, ...]] = {
    EntityID.Player: tuple(PlayerAttributeID),
    EntityID.Queue: tuple(QueueAttributeID),
    EntityID.Track: tuple(TrackAttributeID),
}

# Stable, public command names used by D-Bus, the CLI and MPRIS mapping.
COMMAND_NAMES: dict[str, RemoteCommandID] = {
    "play": RemoteCommandID.Play,
    "pause": RemoteCommandID.Pause,
    "toggle": RemoteCommandID.TogglePlayPause,
    "next": RemoteCommandID.NextTrack,
    "previous": RemoteCommandID.PreviousTrack,
    "volume-up": RemoteCommandID.VolumeUp,
    "volume-down": RemoteCommandID.VolumeDown,
    "repeat": RemoteCommandID.AdvanceRepeatMode,
    "shuffle": RemoteCommandID.AdvanceShuffleMode,
    "skip-forward": RemoteCommandID.SkipForward,
    "skip-backward": RemoteCommandID.SkipBackward,
    "like": RemoteCommandID.LikeTrack,
    "dislike": RemoteCommandID.DislikeTrack,
    "bookmark": RemoteCommandID.BookmarkTrack,
}
COMMAND_IDS: dict[RemoteCommandID, str] = {
    value: name for name, value in COMMAND_NAMES.items()
}
