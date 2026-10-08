"""AMS wire-format parsing and command encoding, without D-Bus or BlueZ."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry.ams.constants import (
    COMMAND_IDS,
    COMMAND_NAMES,
    ENTITY_ATTRIBUTES,
    EntityID,
    PlaybackState,
    RemoteCommandID,
    TrackAttributeID,
)
from blueferry.ams.parsers import (
    EntityUpdate,
    build_entity_attribute_request,
    build_entity_update_registration,
    build_remote_command,
    clean_text,
    decode_attribute_value,
    parse_playback_info,
    parse_supported_commands,
    parse_volume,
)
from blueferry.ams.state import NowPlaying
from blueferry.limits import MAX_AMS_VALUE_BYTES, MAX_REMOTE_PROPERTY_CHARS

PROPERTY_SETTINGS = settings(max_examples=200, derandomize=True, deadline=None)


def test_entity_update_exact_layout() -> None:
    update = EntityUpdate.parse(bytes([2, 2, 1]) + b"Hey Jude")

    assert update == EntityUpdate(
        entity=EntityID.Track,
        attribute=TrackAttributeID.Title,
        truncated=True,
        value="Hey Jude",
    )


def test_truncated_value_drops_a_split_multibyte_remnant() -> None:
    encoded = "Café".encode()[:-1]  # cut inside the two-byte "é"
    assert EntityUpdate.parse(bytes([2, 2, 1]) + encoded).value == "Caf"
    # A complete value keeps a replacement character that the phone sent.
    assert EntityUpdate.parse(bytes([2, 2, 0]) + encoded).value == "Caf\ufffd"


def test_entity_update_rejects_short_and_oversized_packets() -> None:
    with pytest.raises(ValueError):
        EntityUpdate.parse(b"\x02\x02")
    with pytest.raises(ValueError):
        EntityUpdate.parse(b"\x02\x02\x00" + b"a" * (MAX_AMS_VALUE_BYTES + 1))


@PROPERTY_SETTINGS
@given(st.binary(max_size=MAX_AMS_VALUE_BYTES + 16))
def test_entity_update_parser_only_raises_value_error(data: bytes) -> None:
    try:
        update = EntityUpdate.parse(data)
    except ValueError:
        return
    assert update.entity == data[0]
    assert update.attribute == data[1]
    assert update.truncated == bool(data[2] & 1)
    assert isinstance(update.value, str)


@PROPERTY_SETTINGS
@given(
    st.sampled_from(list(EntityID)),
    st.integers(0, 255),
    st.integers(0, 255),
    st.text(max_size=400),
)
def test_entity_update_round_trips_utf8_values(entity, attribute, flags, value) -> None:
    encoded = value.encode("utf-8", errors="surrogatepass")
    try:
        encoded.decode("utf-8")
    except UnicodeDecodeError:
        return
    update = EntityUpdate.parse(bytes([entity, attribute, flags]) + encoded)
    assert update.value == (value.rstrip("\ufffd") if flags & 1 else value)
    assert update.truncated == bool(flags & 1)


@PROPERTY_SETTINGS
@given(st.binary(max_size=64), st.floats(0, 1e6, allow_nan=False))
def test_now_playing_never_raises_on_arbitrary_updates(data: bytes, now: float) -> None:
    state = NowPlaying()
    try:
        update = EntityUpdate.parse(data)
    except ValueError:
        return
    state.apply(update, now)
    snapshot = state.snapshot(now)
    for section in ("player", "queue", "track"):
        assert isinstance(snapshot[section], dict)
    for text in (state.title, state.artist, state.album, state.player_name):
        assert text is None or len(text) <= MAX_REMOTE_PROPERTY_CHARS


@PROPERTY_SETTINGS
@given(st.binary(max_size=300))
def test_supported_commands_ignore_unknown_identifiers(data: bytes) -> None:
    try:
        commands = parse_supported_commands(data)
    except ValueError:
        assert len(data) > 256
        return
    assert commands == {RemoteCommandID(value) for value in data if value <= 13}


def test_supported_commands_example() -> None:
    assert parse_supported_commands(bytes([0, 1, 2, 3, 4, 200])) == {
        RemoteCommandID.Play,
        RemoteCommandID.Pause,
        RemoteCommandID.TogglePlayPause,
        RemoteCommandID.NextTrack,
        RemoteCommandID.PreviousTrack,
    }
    assert parse_supported_commands(b"") == frozenset()


@pytest.mark.parametrize("command", list(RemoteCommandID))
def test_every_remote_command_encodes_as_its_single_byte(command) -> None:
    assert build_remote_command(command) == bytes([int(command)])


def test_remote_command_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        build_remote_command(14)


def test_public_command_names_cover_every_command_once() -> None:
    assert set(COMMAND_NAMES.values()) == set(RemoteCommandID)
    assert all(COMMAND_NAMES[name] == command for command, name in COMMAND_IDS.items())


def test_registration_packets_follow_the_spec() -> None:
    assert build_entity_update_registration(EntityID.Player, (0, 1, 2)) == b"\x00\x00\x01\x02"
    assert build_entity_update_registration(EntityID.Queue, (0, 1, 2, 3)) == b"\x01\x00\x01\x02\x03"
    assert build_entity_update_registration(EntityID.Track, (0, 1, 2, 3)) == b"\x02\x00\x01\x02\x03"
    for entity, attributes in ENTITY_ATTRIBUTES.items():
        assert build_entity_update_registration(entity, attributes)[0] == entity


@pytest.mark.parametrize("entity,attributes", [
    (3, (0,)),
    (EntityID.Player, ()),
    (EntityID.Player, (3,)),
    (EntityID.Track, (4,)),
])
def test_registration_rejects_invalid_selectors(entity, attributes) -> None:
    with pytest.raises(ValueError):
        build_entity_update_registration(entity, attributes)


def test_entity_attribute_selector_and_value() -> None:
    assert build_entity_attribute_request(EntityID.Track, TrackAttributeID.Title) == b"\x02\x02"
    with pytest.raises(ValueError):
        build_entity_attribute_request(EntityID.Queue, 9)
    assert decode_attribute_value("Å long title".encode()) == "Å long title"
    with pytest.raises(ValueError):
        decode_attribute_value(b"a" * (MAX_AMS_VALUE_BYTES + 1))


@pytest.mark.parametrize("value,state,rate,elapsed", [
    ("1,1.0,12.5", PlaybackState.Playing, 1.0, 12.5),
    ("0,0.0,99.000", PlaybackState.Paused, 0.0, 99.0),
    ("3,2.0,5", PlaybackState.FastForwarding, 2.0, 5.0),
    ("9,1.0,1", None, 1.0, 1.0),
    ("", None, None, None),
    ("1,,", PlaybackState.Playing, None, None),
])
def test_playback_info(value, state, rate, elapsed) -> None:
    info = parse_playback_info(value)
    assert (info.state, info.rate, info.elapsed) == (state, rate, elapsed)


@pytest.mark.parametrize("value", ["1,1.0", "1,nan,2", "1,1,inf", "1,1,-4", "x,1,2,3", "1,5000,1"])
def test_playback_info_rejects_malformed(value) -> None:
    with pytest.raises(ValueError):
        parse_playback_info(value)


@PROPERTY_SETTINGS
@given(st.text(max_size=64))
def test_playback_info_only_raises_value_error(value: str) -> None:
    try:
        parse_playback_info(value)
    except ValueError:
        pass


def test_volume_bounds() -> None:
    assert parse_volume("0.5") == 0.5
    assert parse_volume("") is None
    with pytest.raises(ValueError):
        parse_volume("1.5")


@PROPERTY_SETTINGS
@given(st.text(max_size=2_000))
def test_clean_text_removes_controls_and_bounds_length(value: str) -> None:
    cleaned = clean_text(value)
    assert len(cleaned) <= MAX_REMOTE_PROPERTY_CHARS
    assert not any(ord(character) < 0x20 or character == "\x7f" for character in cleaned)
    assert not any(
        0x202A <= ord(character) <= 0x202E or 0x2066 <= ord(character) <= 0x2069
        or character in "\u200e\u200f"
        for character in cleaned
    )


def test_clean_text_keeps_emoji_sequences_and_unassigned_code_points() -> None:
    family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"  # ZWJ family
    assert clean_text(f"Song {family}") == f"Song {family}"
    future = "\U0001FAFF"  # unassigned in older Unicode databases
    assert clean_text(f"New {future}") == f"New {future}"
    assert clean_text("\u202eevil\u202c\u200f name\x07") == "evil name"


def test_truncated_value_is_replaced_by_full_read() -> None:
    state = NowPlaying()
    state.apply(EntityUpdate(EntityID.Track, TrackAttributeID.Title, True, "A very lo"), 0.0)
    assert state.title == "A very lo"
    state.apply(EntityUpdate(EntityID.Track, TrackAttributeID.Title, False, "A very long title"), 0.0)
    assert state.title == "A very long title"


def test_malformed_known_value_keeps_previous_state() -> None:
    state = NowPlaying()
    state.apply(EntityUpdate(EntityID.Player, 2, False, "0.25"), 0.0)
    assert state.apply(EntityUpdate(EntityID.Player, 2, False, "loud"), 0.0) is False
    assert state.volume == 0.25


def test_position_extrapolates_only_while_playing_and_clamps() -> None:
    state = NowPlaying()
    state.apply(EntityUpdate(EntityID.Track, TrackAttributeID.Duration, False, "100"), 0.0)
    state.apply(EntityUpdate(EntityID.Player, 1, False, "1,1.0,10"), 50.0)
    assert state.position(55.0) == pytest.approx(15.0)
    assert state.position(500.0) == pytest.approx(100.0)
    state.apply(EntityUpdate(EntityID.Player, 1, False, "0,0.0,20"), 60.0)
    assert state.position(90.0) == pytest.approx(20.0)


def test_snapshot_shape() -> None:
    state = NowPlaying()
    for entity, attribute, value in (
        (0, 0, "Music"), (0, 1, "1,1.0,3"), (0, 2, "0.5"),
        (1, 0, "2"), (1, 1, "10"), (1, 2, "1"), (1, 3, "2"),
        (2, 0, "Artist"), (2, 1, "Album"), (2, 2, "Title"), (2, 3, "180.5"),
    ):
        state.apply(EntityUpdate(entity, attribute, False, value), 0.0)
    state.set_supported_commands(frozenset({RemoteCommandID.Play, RemoteCommandID.NextTrack}))

    assert state.snapshot(2.0) == {
        "player": {"name": "Music", "state": "playing", "rate": 1.0, "volume": 0.5},
        "queue": {"index": 2, "count": 10, "shuffle": "one", "repeat": "all"},
        "track": {
            "artist": "Artist", "album": "Album", "title": "Title",
            "duration": 180.5, "elapsed": 5.0,
        },
        "supported_commands": ["next", "play"],
    }
    state.reset()
    assert state.title is None and state.supported_commands == frozenset()
