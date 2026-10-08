"""Optional MPRIS2 player for the iPhone's now-playing state.

Plasma's media controller, media keys, KDE Connect-style widgets and
``playerctl`` discover players by the ``org.mpris.MediaPlayer2.*`` bus-name
prefix. This adapter publishes the iPhone as one such player while AMS reports
an active media app, and releases the name otherwise so desktops do not show a
permanently stopped entry.

Privacy: MPRIS is a public, session-wide interface. Every application in the
login session can read ``Metadata`` (title, artist, album) and receives
``PropertiesChanged`` broadcasts, exactly as with any desktop music player.
That is why it is a separate opt-in (``BLUEFERRY_MEDIA_MPRIS_ENABLED``) on top
of media control, whose own Media1 API keeps details behind BlueFerry's
authenticated, rate-limited calls and a content-free signal. Method and
property calls still pass the same caller UID check and media rate buckets.

Isolation: the player lives on its own private session-bus connection. A
well-known name addresses a connection, not an object, so exporting it on the
daemon's main connection would let any client allowed to talk to
``org.mpris.MediaPlayer2.*`` (for example a Flatpak app behind
xdg-dbus-proxy) reach ``/io/weirdware/BlueFerry`` and its Messages1 methods
through that name. Only the MPRIS object is exported on this connection.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import dbus
import dbus.bus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service

from blueferry.ams.constants import PlaybackState, RemoteCommandID
from blueferry.dbus_security import CallerGuard
from blueferry.errors import BlueFerryError
from blueferry.media import MediaController

log = logging.getLogger(__name__)

MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.blueferry_iphone"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_IFACE = dbus.PROPERTIES_IFACE
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
TRACK_PATH_PREFIX = "/io/weirdware/BlueFerry/MediaPlayer/Track"
IDENTITY = "iPhone (BlueFerry)"
# Basename of the Kirigami client's installed desktop file; desktops use it
# for the player's icon and name.
DESKTOP_ENTRY = "io.weirdware.BlueFerry.Qt"
_DBUS_NAME = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
NAME_CALL_TIMEOUT_SECONDS = 10
# A reported position farther than this from the extrapolated one is a seek.
SEEK_THRESHOLD_SECONDS = 1.5

_ROOT_PROPERTIES = {
    "CanQuit": "b",
    "CanRaise": "b",
    "HasTrackList": "b",
    "Identity": "s",
    "DesktopEntry": "s",
    "SupportedUriSchemes": "as",
    "SupportedMimeTypes": "as",
}
_PLAYER_PROPERTIES = {
    "PlaybackStatus": "s",
    "Rate": "d",
    "Metadata": "a{sv}",
    "Volume": "d",
    "Position": "x",
    "MinimumRate": "d",
    "MaximumRate": "d",
    "CanGoNext": "b",
    "CanGoPrevious": "b",
    "CanPlay": "b",
    "CanPause": "b",
    "CanSeek": "b",
    "CanControl": "b",
}
_WRITABLE = {(PLAYER_IFACE, "Volume")}


class _UnknownProperty(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownProperty"


class _UnknownInterface(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.UnknownInterface"


class _ReadOnly(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.PropertyReadOnly"


class _NotSupported(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.NotSupported"


def _property_xml(properties: dict[str, str], writable: set[str]) -> str:
    return "".join(
        f'    <property name="{name}" type="{signature}" '
        f'access="{"readwrite" if name in writable else "read"}"/>\n'
        for name, signature in properties.items()
    )


def private_session_bus() -> dbus.connection.Connection:
    """A session-bus connection of its own, dispatched on the GLib loop.

    The caller keeps it for the process lifetime: closing a dbus-python
    connection while replies are still queued for it trips an assertion in
    the dispatcher, and the bus drops its names when the process exits.
    """
    return dbus.SessionBus(
        private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop(),
    )


class NameClaim:
    """Own one bus name on one connection, one bus call at a time.

    The daemon keeps the private connection, and so the name, across players:
    a player closed during its ``RequestName`` must not release the name its
    successor has just been told it holds. Requests and releases are therefore
    serialised here, and only the latest claimant hears the outcome.
    """

    def __init__(self, connection: dbus.connection.Connection, name: str) -> None:
        self._connection = connection
        self._name = name
        self._wanted = False
        self._held = False
        self._busy = False
        self._listener: Callable[[bool], None] | None = None

    def want(self, wanted: bool, listener: Callable[[bool], None] | None = None) -> None:
        """Ask for or give up the name; ``listener(held)`` reports a request once."""
        self._wanted = wanted
        self._listener = listener if wanted else None
        self._advance()

    def _report(self, held: bool) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener(held)

    def _call(self, method: str, args: tuple, signature: str, reply, error) -> None:
        self._busy = True
        self._connection.call_async(
            _DBUS_NAME, _DBUS_PATH, _DBUS_NAME, method, signature, args,
            reply, error, timeout=NAME_CALL_TIMEOUT_SECONDS,
        )

    def _advance(self) -> None:
        if self._busy:
            return
        if self._wanted == self._held:
            if self._held:
                self._report(True)
            return
        if self._wanted:
            self._call(
                "RequestName",
                (self._name, dbus.UInt32(dbus.bus.NAME_FLAG_DO_NOT_QUEUE)),
                "su", self._requested, self._request_failed,
            )
        else:
            self._call("ReleaseName", (self._name,), "s", self._released, self._released)

    def _requested(self, result) -> None:
        self._busy = False
        self._held = int(result) in (
            dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER,
            dbus.bus.REQUEST_NAME_REPLY_ALREADY_OWNER,
        )
        if not self._held and self._wanted:
            log.warning("MPRIS player name is owned by another process")
            self._wanted = False
            self._report(False)
        self._advance()

    def _request_failed(self, error) -> None:
        self._busy = False
        name = (
            error.get_dbus_name()
            if isinstance(error, dbus.exceptions.DBusException)
            else type(error).__name__
        )
        if self._wanted:
            log.warning("could not publish MPRIS player: %s", name)
            self._wanted = False
            self._report(False)
        self._advance()

    def _released(self, _result=None) -> None:
        # A failed release leaves nothing to retry: the bus drops the name
        # with the connection at the latest.
        self._busy = False
        self._held = False
        self._advance()


class MprisPlayer(dbus.service.Object):
    """Map MPRIS2 to :class:`MediaController`, and own the name while active."""

    def __init__(
        self,
        connection: dbus.connection.Connection,
        media: MediaController,
        caller_guard: CallerGuard,
        *,
        clock: Callable[[], float] = time.monotonic,
        bus_name: str = MPRIS_BUS_NAME,
        claim: NameClaim | None = None,
    ) -> None:
        # Exported only while the player name is owned or being requested.
        super().__init__()
        # Must be a private connection (private_session_bus), never the
        # daemon's main one; see the module docstring.
        self._connection = connection
        self._media = media
        self._guard = caller_guard
        self._clock = clock
        # Shared by every player on this connection; see NameClaim.
        self._claim = claim or NameClaim(connection, bus_name)
        self._owned = False
        self._exported = False
        self._claiming = False
        self._last_properties: dict[str, object] = {}
        self._track_serial: int | None = None
        # Last non-zero rate the phone reported; MPRIS forbids Rate 0.
        self._rate = 1.0
        self._last_position: tuple[float, float, float] | None = None
        self._closed = False
        media.add_listener(self.refresh)
        self.refresh()

    # ---- ownership and change propagation -------------------------------

    @property
    def owned(self) -> bool:
        return self._owned

    def _should_own(self) -> bool:
        state = self._media.state
        return self._media.available and (
            state.playback_state is not None or state.title is not None
        )

    def refresh(self) -> None:
        """Called after each coalesced now-playing change."""
        if self._closed:
            return
        self._update_track_identity()
        if not self._should_own():
            self._release()
            return
        if not self._owned:
            self._acquire()
            return
        current = self._player_properties()
        changed = {
            name: value
            for name, value in current.items()
            if self._last_properties.get(name) != value
        }
        self._last_properties = current
        if changed:
            self.PropertiesChanged(
                PLAYER_IFACE,
                dbus.Dictionary(changed, signature="sv"),
                dbus.Array([], signature="s"),
            )
        self._maybe_emit_seeked()

    def _export(self) -> None:
        if not self._exported:
            self.add_to_connection(self._connection, MPRIS_PATH)
            self._exported = True

    def _unexport(self) -> None:
        if not self._exported:
            return
        self._exported = False
        try:
            self.remove_from_connection()
        except Exception:
            log.debug("could not unexport MPRIS player", exc_info=True)

    def _acquire(self) -> None:
        """Request the name asynchronously; the GLib loop never waits."""
        if self._claiming or self._owned:
            return
        self._claiming = True
        # Export first so the object answers as soon as the name appears.
        self._export()
        self._claim.want(True, self._claimed)

    def _claimed(self, held: bool) -> None:
        self._claiming = False
        if not held:
            self._unexport()
            return
        self._owned = True
        self._last_properties = self._player_properties()
        self._remember_position()
        log.info("published iPhone MPRIS player")
        # The player may have stopped while the request was in flight.
        if not self._should_own():
            self._release()

    def _release(self) -> None:
        owned, claiming = self._owned, self._claiming
        self._owned = self._claiming = False
        self._last_properties = {}
        self._last_position = None
        self._unexport()
        if owned or claiming:
            self._claim.want(False)
        if owned:
            log.info("withdrew iPhone MPRIS player")

    @property
    def connection(self) -> dbus.connection.Connection:
        return self._connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._media.remove_listener(self.refresh)
        self._release()

    def _update_track_identity(self) -> None:
        # NowPlaying decides what a new track is: completing a truncated
        # title keeps the serial, and with it mpris:trackid, stable.
        serial = self._media.state.track_serial
        if serial != self._track_serial:
            self._track_serial = serial
            self._last_position = None

    def _remember_position(self) -> None:
        state = self._media.state
        if state.elapsed is None or state.elapsed_at is None:
            self._last_position = None
            return
        rate = (state.playback_rate or 0.0) if state.playing else 0.0
        self._last_position = (state.elapsed, state.elapsed_at, rate)

    def _maybe_emit_seeked(self) -> None:
        previous = self._last_position
        self._remember_position()
        current = self._last_position
        if previous is None or current is None or current[:2] == previous[:2]:
            return
        elapsed, at, rate = previous
        expected = elapsed + rate * max(0.0, current[1] - at)
        if abs(current[0] - expected) > SEEK_THRESHOLD_SECONDS:
            self.Seeked(dbus.Int64(self._position_us()))

    # ---- property values ------------------------------------------------

    def _position_us(self) -> int:
        position = self._media.state.position(self._clock())
        return int((position or 0.0) * 1_000_000)

    def _metadata(self) -> dbus.Dictionary:
        state = self._media.state
        metadata: dict[str, object] = {
            "mpris:trackid": dbus.ObjectPath(
                f"{TRACK_PATH_PREFIX}/{state.track_serial}"
                if state.title or state.artist or state.album else NO_TRACK
            ),
        }
        if state.title:
            metadata["xesam:title"] = dbus.String(state.title)
        if state.artist:
            metadata["xesam:artist"] = dbus.Array([state.artist], signature="s")
        if state.album:
            metadata["xesam:album"] = dbus.String(state.album)
        if state.duration:
            metadata["mpris:length"] = dbus.Int64(int(state.duration * 1_000_000))
        return dbus.Dictionary(metadata, signature="sv")

    def _supports(self, *commands: RemoteCommandID) -> bool:
        supported = self._media.state.supported_commands
        return self._media.available and any(command in supported for command in commands)

    def _current_rate(self) -> float:
        """The phone's playback rate (podcasts at 1.5x), never 0 or negative.

        MPRIS clients extrapolate Position with Rate, and the specification
        forbids 0; while paused or rewinding the last forward rate is kept.
        """
        rate = self._media.state.playback_rate
        if self._media.state.playing and rate is not None and rate > 0:
            self._rate = rate
        return self._rate

    def _player_properties(self) -> dict[str, object]:
        state = self._media.state
        if not self._media.available or state.playback_state is None:
            status = "Stopped"
        elif state.playback_state == PlaybackState.Paused:
            status = "Paused"
        else:
            status = "Playing"
        toggle = RemoteCommandID.TogglePlayPause
        rate = self._current_rate()
        values: dict[str, object] = {
            "PlaybackStatus": dbus.String(status),
            "Rate": dbus.Double(rate),
            "Metadata": self._metadata(),
            # Rate is read-only here; the bounds only have to contain it.
            "MinimumRate": dbus.Double(min(1.0, rate)),
            "MaximumRate": dbus.Double(max(1.0, rate)),
            "CanGoNext": dbus.Boolean(self._supports(RemoteCommandID.NextTrack)),
            "CanGoPrevious": dbus.Boolean(self._supports(RemoteCommandID.PreviousTrack)),
            "CanPlay": dbus.Boolean(self._supports(RemoteCommandID.Play, toggle)),
            "CanPause": dbus.Boolean(self._supports(RemoteCommandID.Pause, toggle)),
            "CanSeek": dbus.Boolean(False),
            "CanControl": dbus.Boolean(True),
        }
        # Until the phone reports its volume, leave Volume out rather than
        # claim 0.0 (muted); MPRIS clients then hide their volume control.
        if state.volume is not None:
            values["Volume"] = dbus.Double(state.volume)
        return values

    def _root_properties(self) -> dict[str, object]:
        return {
            "CanQuit": dbus.Boolean(False),
            "CanRaise": dbus.Boolean(False),
            "HasTrackList": dbus.Boolean(False),
            "Identity": dbus.String(IDENTITY),
            "DesktopEntry": dbus.String(DESKTOP_ENTRY),
            "SupportedUriSchemes": dbus.Array([], signature="s"),
            "SupportedMimeTypes": dbus.Array([], signature="s"),
        }

    def _all(self, interface: str) -> dict[str, object]:
        if interface == ROOT_IFACE:
            return self._root_properties()
        if interface == PLAYER_IFACE:
            values = self._player_properties()
            values["Position"] = dbus.Int64(self._position_us())
            return values
        raise _UnknownInterface(f"no such interface: {interface}")

    def _authorize(self, sender, action: str) -> None:
        try:
            self._guard.authorize(sender, action)
        except BlueFerryError as error:
            raise dbus.exceptions.DBusException(
                str(error), name=f"io.weirdware.BlueFerry.Error.{error.dbus_suffix}",
            ) from None

    # ---- org.freedesktop.DBus.Properties --------------------------------

    @dbus.service.method(
        PROPERTIES_IFACE, in_signature="ss", out_signature="v", sender_keyword="sender",
    )
    def Get(self, interface: str, name: str, sender=None):
        self._authorize(sender, "media-read")
        values = self._all(str(interface))
        if name not in values:
            raise _UnknownProperty(f"no such property: {name}")
        return values[name]

    @dbus.service.method(
        PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}", sender_keyword="sender",
    )
    def GetAll(self, interface: str, sender=None):
        self._authorize(sender, "media-read")
        return dbus.Dictionary(self._all(str(interface)), signature="sv")

    @dbus.service.method(
        PROPERTIES_IFACE, in_signature="ssv", out_signature="", sender_keyword="sender",
    )
    def Set(self, interface: str, name: str, value, sender=None) -> None:
        self._authorize(sender, "media-command")
        if (str(interface), str(name)) not in _WRITABLE:
            if str(name) in self._all(str(interface)):
                raise _ReadOnly(f"property is read-only: {name}")
            raise _UnknownProperty(f"no such property: {name}")
        # AMS has no absolute volume; move one iPhone volume step toward the
        # requested level. Plasma sends a new value per scroll step.
        current = self._media.state.volume
        try:
            target = float(value)
        except (TypeError, ValueError):
            raise dbus.exceptions.DBusException(
                "volume must be a number", name="org.freedesktop.DBus.Error.InvalidArgs",
            ) from None
        if current is None or abs(target - current) < 0.01:
            return
        self._command("volume-up" if target > current else "volume-down")

    @dbus.service.signal(PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        """Standard property change notification (MPRIS clients rely on it)."""

    # ---- org.mpris.MediaPlayer2 -----------------------------------------

    @dbus.service.method(ROOT_IFACE, in_signature="", out_signature="")
    def Raise(self) -> None:
        """CanRaise is false; the iPhone has no desktop window to raise."""

    @dbus.service.method(ROOT_IFACE, in_signature="", out_signature="")
    def Quit(self) -> None:
        """CanQuit is false; the backend lifetime is not the player's."""

    # ---- org.mpris.MediaPlayer2.Player ----------------------------------

    def _command(self, name: str) -> None:
        """Best effort: MPRIS says unsupported actions have no effect."""
        try:
            self._media.send_command(
                name,
                lambda: None,
                lambda error: log.info("MPRIS media command failed: %s", type(error).__name__),
            )
        except BlueFerryError as error:
            log.debug("MPRIS media command ignored: %s", error)

    def _player_call(self, sender, name: str) -> None:
        self._authorize(sender, "media-command")
        self._command(name)

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Next(self, sender=None) -> None:
        self._player_call(sender, "next")

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Previous(self, sender=None) -> None:
        self._player_call(sender, "previous")

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Pause(self, sender=None) -> None:
        self._player_call(sender, "pause")

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def PlayPause(self, sender=None) -> None:
        self._player_call(sender, "toggle")

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Stop(self, sender=None) -> None:
        # AMS has no stop; pausing is the closest non-destructive action.
        self._player_call(sender, "pause")

    @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Play(self, sender=None) -> None:
        self._player_call(sender, "play")

    @dbus.service.method(PLAYER_IFACE, in_signature="x", out_signature="", sender_keyword="sender")
    def Seek(self, offset: int, sender=None) -> None:
        """CanSeek is false, so MPRIS requires Seek to have no effect.

        AMS fixed skips remain available through Media1 and the CLI.
        """
        self._authorize(sender, "media-command")

    @dbus.service.method(PLAYER_IFACE, in_signature="ox", out_signature="", sender_keyword="sender")
    def SetPosition(self, track_id, position: int, sender=None) -> None:
        """CanSeek is false; absolute positioning is not available over AMS."""
        self._authorize(sender, "media-command")

    @dbus.service.method(PLAYER_IFACE, in_signature="s", out_signature="", sender_keyword="sender")
    def OpenUri(self, uri: str, sender=None) -> None:
        self._authorize(sender, "media-command")
        raise _NotSupported("the iPhone player cannot open URIs")

    @dbus.service.signal(PLAYER_IFACE, signature="x")
    def Seeked(self, position):
        """The iPhone reported a position discontinuity (in microseconds)."""

    # ---- introspection with properties ----------------------------------

    @dbus.service.method(
        dbus.INTROSPECTABLE_IFACE, in_signature="", out_signature="s",
        path_keyword="object_path", connection_keyword="connection",
    )
    def Introspect(self, object_path, connection):
        xml = dbus.service.Object.Introspect(self, object_path, connection)
        # Declare what Get answers: Volume is absent until the phone reports it.
        player = {
            name: signature for name, signature in _PLAYER_PROPERTIES.items()
            if name != "Volume" or self._media.state.volume is not None
        }
        for interface, properties, writable in (
            (ROOT_IFACE, _ROOT_PROPERTIES, set()),
            (PLAYER_IFACE, player, {"Volume"}),
        ):
            marker = f'  <interface name="{interface}">\n'
            xml = xml.replace(marker, marker + _property_xml(properties, writable), 1)
        return xml
