"""Media1 and MPRIS round trips on the isolated dbus-run-session test bus.

Nothing here reaches BlueZ: the AMS client is replaced by an inert command
writer and now-playing updates are injected as parsed AMS values.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time

import dbus
import dbus.mainloop
import dbus.mainloop.glib
import dbus.service
import pytest
from gi.repository import GLib

from blueferry.ams.constants import EntityID, RemoteCommandID
from blueferry.ams.parsers import EntityUpdate
from blueferry.backend_operations import BackendDependencies
from blueferry.dbus_service import MessagesService
from blueferry.media import MediaController
from blueferry.mpris import (
    MPRIS_PATH,
    PLAYER_IFACE,
    ROOT_IFACE,
    MprisPlayer,
    private_session_bus,
)
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, MEDIA_IFACE, MESSAGES_IFACE, OBJECT_PATH

pytestmark = pytest.mark.private_dbus
_ids = itertools.count()


class _Writer:
    available = True

    def __init__(self) -> None:
        self.sent: list[RemoteCommandID] = []

    def send_command(self, command, on_success, _on_failure) -> None:
        self.sent.append(command)
        on_success()


class _Sessions:
    map = None
    pbap = None
    map_path = ""

    @staticmethod
    def report_error(_error) -> None:
        pass


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


def _call(name, path, interface, method, *args):
    """Synchronous call from a worker thread while the test loop dispatches."""
    outcome: dict = {}

    def run() -> None:
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            proxy = dbus.Interface(connection.get_object(name, path, introspect=False), interface)
            outcome["value"] = getattr(proxy, method)(*args, timeout=5)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join()
    return outcome


_controllers: list[MediaController] = []


@pytest.fixture(autouse=True)
def _close_controllers():
    """Cancel each controller's pending GLib coalescing timer after a test."""
    yield
    while _controllers:
        _controllers.pop().close()


def _media():
    media = MediaController(clock=lambda: 10.0)
    _controllers.append(media)
    writer = _Writer()
    media.attach(writer)
    media.handle_supported_commands(frozenset({
        RemoteCommandID.TogglePlayPause, RemoteCommandID.NextTrack,
        RemoteCommandID.PreviousTrack, RemoteCommandID.VolumeUp,
        RemoteCommandID.VolumeDown,
    }))
    return media, writer


def _play(media, title="Title") -> None:
    for entity, attribute, value in (
        (EntityID.Player, 0, "Music"),
        (EntityID.Player, 1, "1,1.0,30"),
        (EntityID.Player, 2, "0.5"),
        (EntityID.Track, 0, "Artist"),
        (EntityID.Track, 1, "Album"),
        (EntityID.Track, 2, title),
        (EntityID.Track, 3, "240"),
    ):
        media.handle_update(EntityUpdate(entity, attribute, False, value))


@pytest.fixture
def service_factory():
    created = []

    def make(media, **dependencies):
        bus = dbus.SessionBus()
        name = f"{BUS_NAME}.Mediap{os.getpid()}n{next(_ids)}"
        bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
        service = MessagesService(
            bus_name, _Sessions(),
            BackendDependencies(media=lambda: media, **dependencies),
        )
        if media is not None:
            media.add_listener(service.emit_now_playing_changed)
        created.append((bus, name, service))
        return name, service

    yield make
    for bus, name, service in created:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def test_media1_is_inert_when_disabled(service_factory) -> None:
    name, _ = service_factory(None)
    result = _call(name, OBJECT_PATH, MEDIA_IFACE, "GetNowPlaying")
    assert json.loads(result["value"]) == {
        "enabled": False, "available": False, "detail": "disabled",
    }
    result = _call(name, OBJECT_PATH, MEDIA_IFACE, "SendMediaCommand", "play")
    assert result["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"


def test_media1_snapshot_command_and_content_free_signal(service_factory) -> None:
    media, writer = _media()
    name, _service = service_factory(media)
    listener = dbus.SessionBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
    received = []
    listener.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=EVENTS_IFACE, signal_name="NowPlayingChanged",
    )
    try:
        _play(media)
        _dispatch_until(lambda: received)
        assert received == [()]  # no track, artist, or state on the broadcast

        snapshot = json.loads(_call(name, OBJECT_PATH, MEDIA_IFACE, "GetNowPlaying")["value"])
        assert snapshot["available"] is True
        assert snapshot["track"]["title"] == "Title"
        assert snapshot["player"]["state"] == "playing"

        assert "error" not in _call(name, OBJECT_PATH, MEDIA_IFACE, "SendMediaCommand", "next")
        assert writer.sent == [RemoteCommandID.NextTrack]

        invalid = _call(name, OBJECT_PATH, MEDIA_IFACE, "SendMediaCommand", "format-phone")
        assert invalid["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.InvalidArgs"
        unsupported = _call(name, OBJECT_PATH, MEDIA_IFACE, "SendMediaCommand", "like")
        assert unsupported["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"
        assert writer.sent == [RemoteCommandID.NextTrack]
    finally:
        listener.close()


def test_set_media_control_round_trip(service_factory) -> None:
    calls = []

    def configure(enabled):
        calls.append(enabled)
        return {"media_control_enabled": enabled, "media_control_available": False}

    name, _ = service_factory(None, set_media_control=configure)
    result = _call(name, OBJECT_PATH, MEDIA_IFACE, "SetMediaControl", dbus.Boolean(True))
    assert json.loads(result["value"]) == {
        "media_control_enabled": True, "media_control_available": False,
    }
    assert calls == [True]


def test_set_media_control_without_backend_support_is_not_ready(service_factory) -> None:
    name, _ = service_factory(None)
    result = _call(name, OBJECT_PATH, MEDIA_IFACE, "SetMediaControl", dbus.Boolean(True))
    assert result["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"


# ---- MPRIS ------------------------------------------------------------------


_MPRIS_CONNECTIONS: list = []


def _mpris_connection():
    """One private connection for all MPRIS tests, like the daemon's."""
    if not _MPRIS_CONNECTIONS:
        _MPRIS_CONNECTIONS.append(private_session_bus())
    return _MPRIS_CONNECTIONS[0]


@pytest.fixture
def mpris_factory():
    created = []

    def make(media):
        bus = dbus.SessionBus()
        guard_name = f"{BUS_NAME}.Mprisp{os.getpid()}n{next(_ids)}"
        bus_name = dbus.service.BusName(guard_name, bus=bus, do_not_queue=True)
        service = MessagesService(
            bus_name, _Sessions(), BackendDependencies(media=lambda: media),
        )
        player_name = f"org.mpris.MediaPlayer2.blueferry_test_{os.getpid()}_{next(_ids)}"
        player = MprisPlayer(_mpris_connection(), media, service.caller_guard,
                             clock=lambda: 10.0, bus_name=player_name)
        created.append((bus, guard_name, service, player))
        return player.connection, player_name, player

    yield make
    for bus, guard_name, service, player in created:
        player.close()
        service.close()
        service.remove_from_connection()
        bus.release_name(guard_name)


def _exported(bus) -> bool:
    parent, child = MPRIS_PATH.rsplit("/", 1)
    return child in [str(name) for name in bus.list_exported_child_objects(parent)]


def _owner(bus, name) -> bool:
    return bool(bus.name_has_owner(name))


def test_mpris_name_is_published_only_while_a_player_is_active(mpris_factory) -> None:
    media, _writer = _media()
    bus, name, player = mpris_factory(media)
    assert not player.owned and not _owner(bus, name)

    assert _exported(bus) is False
    _play(media)
    _dispatch_until(lambda: player.owned)
    assert _owner(bus, name)
    assert _exported(bus) is True

    media.attach(None)
    media.handle_availability(False)
    _dispatch_until(lambda: not player.owned)
    _dispatch_until(lambda: not _owner(bus, name))
    # The object is unexported together with the name.
    assert _exported(bus) is False


def test_mpris_properties_and_methods(mpris_factory) -> None:
    media, writer = _media()
    _bus, name, player = mpris_factory(media)
    _play(media)
    _dispatch_until(lambda: player.owned)

    root = _call(name, "/org/mpris/MediaPlayer2", dbus.PROPERTIES_IFACE, "GetAll", ROOT_IFACE)
    assert root["value"]["Identity"] == "iPhone (BlueFerry)"
    assert not root["value"]["CanRaise"]
    assert root["value"]["DesktopEntry"] == "io.weirdware.BlueFerry.Qt"

    props = _call(
        name, "/org/mpris/MediaPlayer2", dbus.PROPERTIES_IFACE, "GetAll", PLAYER_IFACE,
    )["value"]
    assert props["PlaybackStatus"] == "Playing"
    assert props["CanGoNext"] and props["CanPlay"] and props["CanPause"]
    assert not props["CanSeek"]
    assert props["Volume"] == pytest.approx(0.5)
    assert props["Position"] == 30_000_000
    metadata = props["Metadata"]
    assert metadata["xesam:title"] == "Title"
    assert list(metadata["xesam:artist"]) == ["Artist"]
    assert metadata["xesam:album"] == "Album"
    assert metadata["mpris:length"] == 240_000_000
    assert str(metadata["mpris:trackid"]).startswith("/io/weirdware/BlueFerry/MediaPlayer/Track/")

    assert "error" not in _call(name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "PlayPause")
    assert "error" not in _call(name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "Next")
    # Unsupported actions have no effect, as MPRIS requires.
    assert "error" not in _call(name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "Play")
    assert writer.sent == [RemoteCommandID.TogglePlayPause, RemoteCommandID.NextTrack]
    # CanSeek is false: Seek and SetPosition must have no effect.
    assert "error" not in _call(
        name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "Seek", dbus.Int64(15_000_000),
    )
    assert "error" not in _call(
        name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "SetPosition",
        dbus.ObjectPath("/org/mpris/MediaPlayer2/TrackList/NoTrack"), dbus.Int64(0),
    )
    assert writer.sent == [RemoteCommandID.TogglePlayPause, RemoteCommandID.NextTrack]

    # Relative volume: one iPhone step toward the requested level.
    assert "error" not in _call(
        name, "/org/mpris/MediaPlayer2", dbus.PROPERTIES_IFACE, "Set",
        PLAYER_IFACE, "Volume", dbus.Double(0.9, variant_level=1),
    )
    assert writer.sent[-1] == RemoteCommandID.VolumeUp
    read_only = _call(
        name, "/org/mpris/MediaPlayer2", dbus.PROPERTIES_IFACE, "Set",
        PLAYER_IFACE, "PlaybackStatus", dbus.String("Paused", variant_level=1),
    )
    assert read_only["error"].get_dbus_name() == "org.freedesktop.DBus.Error.PropertyReadOnly"

    xml = _call(name, "/org/mpris/MediaPlayer2", dbus.INTROSPECTABLE_IFACE, "Introspect")["value"]
    assert '<property name="Metadata" type="a{sv}" access="read"/>' in xml
    assert '<property name="Volume" type="d" access="readwrite"/>' in xml
    assert '<property name="Identity" type="s" access="read"/>' in xml


def test_mpris_emits_property_changes_for_a_new_track(mpris_factory) -> None:
    media, _writer = _media()
    _bus, name, player = mpris_factory(media)
    _play(media)
    _dispatch_until(lambda: player.owned)
    listener = dbus.SessionBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
    changes = []
    listener.add_signal_receiver(
        lambda interface, changed, _invalidated: changes.append((str(interface), dict(changed))),
        dbus_interface=dbus.PROPERTIES_IFACE, signal_name="PropertiesChanged",
        path="/org/mpris/MediaPlayer2",
    )
    try:
        # Let the AddMatch reach the bus before the change.
        _call(name, "/org/mpris/MediaPlayer2", dbus.PROPERTIES_IFACE, "GetAll", ROOT_IFACE)
        _play(media, title="Second")
        _dispatch_until(lambda: changes)
        interface, changed = changes[0]
        assert interface == PLAYER_IFACE
        assert changed["Metadata"]["xesam:title"] == "Second"
        assert "Position" not in changed  # MPRIS forbids signalling Position
    finally:
        listener.close()


def test_mpris_name_does_not_reach_the_blueferry_object(mpris_factory) -> None:
    """Review #208: a sandbox allowed org.mpris.MediaPlayer2.* must not get Messages1.

    The player has its own connection, so its well-known name addresses only
    the MPRIS object, never /io/weirdware/BlueFerry on the daemon's connection.
    """
    media, _writer = _media()
    bus, name, player = mpris_factory(media)
    _play(media)
    _dispatch_until(lambda: player.owned)
    assert bus is not dbus.SessionBus()

    status = _call(name, OBJECT_PATH, MESSAGES_IFACE, "GetStatus")
    assert status["error"].get_dbus_name() in {
        "org.freedesktop.DBus.Error.UnknownObject",
        "org.freedesktop.DBus.Error.UnknownMethod",
    }
    now_playing = _call(name, OBJECT_PATH, MEDIA_IFACE, "GetNowPlaying")
    assert "error" in now_playing
    # The MPRIS object itself answers through the same name.
    assert "error" not in _call(name, MPRIS_PATH, dbus.PROPERTIES_IFACE, "GetAll", ROOT_IFACE)


def test_mpris_close_releases_the_name(mpris_factory) -> None:
    media, _writer = _media()
    _bus, name, player = mpris_factory(media)
    _play(media)
    _dispatch_until(lambda: player.owned)
    observer = dbus.SessionBus()
    assert observer.name_has_owner(name)
    player.close()
    _dispatch_until(lambda: not observer.name_has_owner(name))
    player.close()  # idempotent


def _player_props(name):
    return _call(name, MPRIS_PATH, dbus.PROPERTIES_IFACE, "GetAll", PLAYER_IFACE)["value"]


def test_mpris_reports_the_phones_playback_rate(mpris_factory) -> None:
    """Review #208: a podcast at 1.5x must not drift in MPRIS clients."""
    media, _writer = _media()
    _bus, name, player = mpris_factory(media)
    _play(media)
    media.handle_update(EntityUpdate(EntityID.Player, 1, False, "1,1.5,30"))
    _dispatch_until(lambda: player.owned)
    props = _player_props(name)
    assert props["Rate"] == pytest.approx(1.5)
    assert props["MinimumRate"] <= 1.0 <= props["MaximumRate"]
    assert props["MaximumRate"] >= props["Rate"]

    # Paused (rate 0) keeps the last forward rate: MPRIS forbids Rate 0.
    media.handle_update(EntityUpdate(EntityID.Player, 1, False, "0,0.0,31"))
    player.refresh()
    props = _player_props(name)
    assert props["PlaybackStatus"] == "Paused"
    assert props["Rate"] == pytest.approx(1.5)


def test_mpris_omits_an_unknown_volume(mpris_factory) -> None:
    """Review #208: unknown volume was reported as 0.0 (muted)."""
    media, writer = _media()
    _bus, name, player = mpris_factory(media)
    media.handle_update(EntityUpdate(EntityID.Player, 1, False, "1,1.0,30"))
    media.handle_update(EntityUpdate(EntityID.Track, 2, False, "Title"))
    _dispatch_until(lambda: player.owned)
    assert "Volume" not in _player_props(name)
    # Setting a volume without a known level sends nothing.
    assert "error" not in _call(
        name, MPRIS_PATH, dbus.PROPERTIES_IFACE, "Set",
        PLAYER_IFACE, "Volume", dbus.Double(0.9, variant_level=1),
    )
    assert writer.sent == []
    media.handle_update(EntityUpdate(EntityID.Player, 2, False, "0.25"))
    player.refresh()
    assert _player_props(name)["Volume"] == pytest.approx(0.25)


def test_mpris_trackid_is_stable_when_a_truncated_title_completes(mpris_factory) -> None:
    """Review #208: completing a long title is not a new track."""
    media, _writer = _media()
    _bus, name, player = mpris_factory(media)
    _play(media, title="A very long")
    media.handle_update(EntityUpdate(EntityID.Track, 2, True, "A very long"))
    _dispatch_until(lambda: player.owned)
    before = _player_props(name)["Metadata"]["mpris:trackid"]

    media.handle_update(EntityUpdate(EntityID.Track, 2, False, "A very long title indeed"))
    player.refresh()
    metadata = _player_props(name)["Metadata"]
    assert metadata["xesam:title"] == "A very long title indeed"
    assert metadata["mpris:trackid"] == before

    _play(media, title="Next song")
    player.refresh()
    assert _player_props(name)["Metadata"]["mpris:trackid"] != before
