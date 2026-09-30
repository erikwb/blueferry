"""Media1 round trips on the isolated dbus-run-session test bus.

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
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, MEDIA_IFACE, OBJECT_PATH

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

    def make(media):
        bus = dbus.SessionBus()
        name = f"{BUS_NAME}.Mediap{os.getpid()}n{next(_ids)}"
        bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
        service = MessagesService(bus_name, _Sessions(), BackendDependencies(media=media))
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
