"""Pure AMS wire-format parsers and command builders.

Every function operates on bytes or strings only, so the complete remote
input surface is testable without D-Bus or BlueZ. Values from the iPhone are
untrusted: malformed input raises ``ValueError`` and nothing else.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from blueferry.ams.constants import (
    ENTITY_ATTRIBUTES,
    EntityID,
    EntityUpdateFlag,
    PlaybackState,
    RemoteCommandID,
)
from blueferry.limits import MAX_AMS_VALUE_BYTES, MAX_REMOTE_PROPERTY_CHARS

_REMOTE_COMMAND_VALUES = frozenset(int(command) for command in RemoteCommandID)


@dataclass(frozen=True, slots=True)
class EntityUpdate:
    """One Entity Update notification: ``[entity][attribute][flags][value…]``."""

    entity: int
    attribute: int
    truncated: bool
    value: str

    @classmethod
    def parse(cls, data: bytes) -> EntityUpdate:
        raw = bytes(data)
        if len(raw) < 3:
            raise ValueError(f"AMS entity update too short ({len(raw)} bytes)")
        if len(raw) - 3 > MAX_AMS_VALUE_BYTES:
            raise ValueError("AMS entity update value is too large")
        entity, attribute, flags = raw[0], raw[1], raw[2]
        truncated = bool(flags & EntityUpdateFlag.Truncated)
        value = raw[3:].decode("utf-8", errors="replace")
        if truncated:
            # Truncation can split a multi-byte character; drop its remnant
            # until the full value arrives through Entity Attribute.
            value = value.rstrip("\ufffd")
        return cls(
            entity=entity,
            attribute=attribute,
            truncated=truncated,
            value=value,
        )


def decode_attribute_value(data: bytes) -> str:
    """Decode a complete Entity Attribute read (the bare UTF-8 value)."""
    raw = bytes(data)
    if len(raw) > MAX_AMS_VALUE_BYTES:
        raise ValueError("AMS entity attribute value is too large")
    return raw.decode("utf-8", errors="replace")


def parse_supported_commands(data: bytes) -> frozenset[RemoteCommandID]:
    """Decode a Remote Command notification into currently available commands.

    Unknown identifiers are future AMS commands; they are ignored rather than
    rejecting the whole list.
    """
    raw = bytes(data)
    if len(raw) > 256:
        raise ValueError("AMS supported-command list is too long")
    return frozenset(
        RemoteCommandID(value) for value in raw if value in _REMOTE_COMMAND_VALUES
    )


def build_remote_command(command: RemoteCommandID | int) -> bytes:
    value = int(command)
    if value not in _REMOTE_COMMAND_VALUES:
        raise ValueError(f"unknown AMS remote command {value}")
    return bytes([value])


def _checked_entity(entity: EntityID | int) -> EntityID:
    try:
        return EntityID(int(entity))
    except ValueError:
        raise ValueError(f"unknown AMS entity {entity}") from None


def build_entity_update_registration(
    entity: EntityID | int, attributes: tuple[int, ...] | list[int],
) -> bytes:
    """Register one entity's attributes; AMS allows one entity per write."""
    selected = _checked_entity(entity)
    allowed = ENTITY_ATTRIBUTES[selected]
    if not attributes:
        raise ValueError("an AMS registration needs at least one attribute")
    for attribute in attributes:
        if int(attribute) not in allowed:
            raise ValueError(f"unknown AMS attribute {attribute} for {selected.name}")
    return bytes([int(selected), *(int(attribute) for attribute in attributes)])


def build_entity_attribute_request(entity: EntityID | int, attribute: int) -> bytes:
    """Select the attribute that the next Entity Attribute read returns in full."""
    selected = _checked_entity(entity)
    if int(attribute) not in ENTITY_ATTRIBUTES[selected]:
        raise ValueError(f"unknown AMS attribute {attribute} for {selected.name}")
    return bytes([int(selected), int(attribute)])


# ---- attribute value semantics -------------------------------------------


# Bidirectional embedding, override and isolate controls plus LRM/RLM can
# visually reorder surrounding UI text. Other format characters such as the
# zero-width joiner are part of emoji sequences and stay.
_BIDI_CONTROLS = frozenset(
    [chr(code) for code in range(0x202A, 0x202F)]
    + [chr(code) for code in range(0x2066, 0x206A)]
    + ["\u200e", "\u200f"]
)


def clean_text(value: str) -> str:
    """Bound remote display text and drop C0/C1 and bidi controls.

    Unassigned code points are kept so emoji newer than this Python's
    Unicode database still display.
    """
    kept = "".join(
        character
        for character in value
        if character not in _BIDI_CONTROLS and unicodedata.category(character) != "Cc"
    )
    return kept.strip()[:MAX_REMOTE_PROPERTY_CHARS]


def _finite(text: str) -> float:
    value = float(text.strip())
    if not math.isfinite(value):
        raise ValueError("AMS numeric value is not finite")
    return value


@dataclass(frozen=True, slots=True)
class PlaybackInfo:
    state: PlaybackState | None
    rate: float | None
    elapsed: float | None


def parse_playback_info(value: str) -> PlaybackInfo:
    """Parse ``PlaybackState,PlaybackRate,ElapsedTime``.

    An empty value means no player is active. Unknown playback states are kept
    as ``None`` so a future iOS state does not look like "paused".
    """
    text = value.strip()
    if not text:
        return PlaybackInfo(None, None, None)
    parts = text.split(",")
    if len(parts) != 3:
        raise ValueError("AMS playback info must have three fields")
    state_text, rate_text, elapsed_text = parts
    state: PlaybackState | None
    try:
        state = PlaybackState(int(state_text.strip()))
    except ValueError:
        state = None
    rate = _finite(rate_text) if rate_text.strip() else None
    elapsed = _finite(elapsed_text) if elapsed_text.strip() else None
    if rate is not None and abs(rate) > 1_000:
        raise ValueError("AMS playback rate is out of range")
    if elapsed is not None and not 0 <= elapsed <= 10 * 365 * 86_400:
        raise ValueError("AMS elapsed time is out of range")
    return PlaybackInfo(state, rate, elapsed)


def parse_volume(value: str) -> float | None:
    if not value.strip():
        return None
    volume = _finite(value)
    if not 0.0 <= volume <= 1.0:
        raise ValueError("AMS volume is out of range")
    return volume


def parse_duration(value: str) -> float | None:
    if not value.strip():
        return None
    duration = _finite(value)
    if not 0.0 <= duration <= 10 * 365 * 86_400:
        raise ValueError("AMS duration is out of range")
    return duration


def parse_count(value: str) -> int | None:
    if not value.strip():
        return None
    number = int(value.strip())
    if not 0 <= number <= 10_000_000:
        raise ValueError("AMS queue value is out of range")
    return number


def parse_mode(value: str) -> int | None:
    """Shuffle/repeat modes; unknown future values become ``None``."""
    if not value.strip():
        return None
    number = int(value.strip())
    return number if number in (0, 1, 2) else None
