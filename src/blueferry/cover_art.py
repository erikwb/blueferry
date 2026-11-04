"""Cover art of the iPhone's now-playing track (AVRCP 1.6 over BIP).

The iPhone offers cover art through the AVRCP "Cover Art" feature: a Basic
Imaging Profile (BIP) OBEX server on a dedicated L2CAP PSM, which BlueZ
publishes as ``org.bluez.MediaPlayer1.ObexPort``. Once an OBEX client is
connected there, the phone adds an image handle (``ImgHandle``, seven
decimal digits) to the track attributes, and obexd's
``org.bluez.obex.Image1.GetThumbnail`` fetches the 200x200 JPEG for it.

Observed on a real iPhone (iOS 27, BlueZ 5.87, see PROTOCOL.md):

* Without a connected BIP session the phone sends no ``ImgHandle`` at all.
* obexd closes a session as soon as the D-Bus client that created it leaves
  the bus. The session therefore lives on a private session-bus connection
  that this module keeps open, and is closed by closing that connection
  (``RemoveSession`` can crash BlueZ 5.87, see ``obex/sessions.py``).
* The phone allows one BIP session at a time; a second ``CreateSession``
  fails with "Connection refused (111)". There is one session per phone,
  not per player, and it is reopened with a backoff after a failure.
* The track already playing when the session opens has no ``ImgHandle``;
  the phone only adds it to attributes requested after the OBEX connect.
  If the player supports browsing, the Now Playing list is read once
  (``MediaFolder1.ChangeFolder`` to the playlist is a local scope change in
  BlueZ, ``ListItems`` asks the phone for the items including their image
  handle). That makes bluetoothd send SetBrowsedPlayer (and possibly
  SetAddressedPlayer), so only the player BlueZ already addresses is
  listed. Without browsing the cover appears from the next track on.

Everything stays local: the images go to an owner-only cache directory and
nothing is sent anywhere. The feature follows the media-control opt-in.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from gi.repository import Gio, GLib

from blueferry.gio_dbus import GioBus, GioDBus, Subscription, call_error, error_name
from blueferry.private_files import PRIVATE_FILE_MODE, ensure_private_directory

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
OBEX = "org.bluez.obex"
OBEX_ROOT = "/org/bluez/obex"
DBUS_NAME = "org.freedesktop.DBus"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
PLAYER_IFACE = "org.bluez.MediaPlayer1"
FOLDER_IFACE = "org.bluez.MediaFolder1"
CONTROL_IFACE = "org.bluez.MediaControl1"
CLIENT_IFACE = "org.bluez.obex.Client1"
IMAGE_IFACE = "org.bluez.obex.Image1"
TRANSFER_IFACE = "org.bluez.obex.Transfer1"
MANAGED_OBJECTS_REPLY = "(a{oa{sa{sv}}})"
# obexd's alias for the AVRCP cover-art BIP target (BIP "Image Pull").
BIP_TARGET = "bip-avrcp"

# BIP 1.2, 4.4.4: an image handle is exactly seven decimal digits.
_IMAGE_HANDLE = re.compile(r"[0-9]{7}", re.ASCII)
# A thumbnail is 200x200 (BIP); generous bounds still stop a broken or
# hostile peer from filling the disk or handing clients a decoder bomb.
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_DIMENSION = 1024
MAX_CACHE_FILES = 20
MAX_CACHE_BYTES = 8 * 1024 * 1024
# Now Playing entries read to find the current track's image handle.
NOW_PLAYING_ITEMS = 50

CREATE_TIMEOUT_SEC = 30.0
CALL_TIMEOUT_SEC = 10.0
TRANSFER_TIMEOUT_SEC = 30
RETRY_DELAYS_SEC = (2, 5, 10, 30, 60)
# iOS removes and recreates its player on an app change and sometimes on
# pause/play. While AVRCP stays connected the session waits this long for a
# player to come back instead of closing and reopening (flapping).
PLAYER_GRACE_SEC = 5
WATCH_RETRY_SEC = 30

_FILE_PREFIX = "cover-"
_INCOMING_PREFIX = ".incoming-"

Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


def valid_image_handle(value: object) -> str | None:
    """The handle if it is a well-formed BIP image handle, else ``None``."""
    if isinstance(value, str) and _IMAGE_HANDLE.fullmatch(value):
        return value
    return None


_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height of a complete baseline/progressive JPEG, else ``None``.

    Walks the marker segments up to the first frame header without decoding
    anything. A truncated transfer lacks the end-of-image marker.
    """
    if len(data) < 4 or not data.startswith(b"\xff\xd8\xff"):
        return None
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        return None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte
            index += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            index += 2
            continue
        if marker in (0xD9, 0xDA):  # end or scan before any frame header
            return None
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > len(data) or length < 7:
                return None
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            if 0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION:
                return width, height
            return None
        index += 2 + length
    return None


def cache_directory() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "")
    # The XDG Base Directory spec says to ignore relative paths.
    if not os.path.isabs(base):
        base = str(Path.home() / ".cache")
    return Path(base) / "blueferry" / "cover"


class CoverCache:
    """Owner-only, bounded cache of validated cover images, keyed by handle.

    Image handles are only meaningful within one BIP session, so the owner
    clears the cache whenever its session ends. File names carry a random
    token: a desktop that caches ``mpris:artUrl`` by URL sees a new URL for
    every new image even if the phone reuses a handle later.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        max_files: int = MAX_CACHE_FILES,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        self._directory = directory
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[Path, int]] = OrderedDict()

    @property
    def directory(self) -> Path:
        return self._directory or cache_directory()

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, handle: str) -> Path | None:
        entry = self._entries.get(handle)
        if entry is None:
            return None
        if not entry[0].is_file():
            self._entries.pop(handle, None)
            return None
        self._entries.move_to_end(handle)
        return entry[0]

    def reserve(self, handle: str) -> Path:
        """Create an empty owner-only file for obexd to write the image to."""
        directory = self.directory
        ensure_private_directory(directory)
        path = directory / f"{_INCOMING_PREFIX}{secrets.token_hex(8)}-{handle}.jpg"
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
        )
        os.close(descriptor)
        return path

    def discard(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            log.debug("could not remove cover file: %s", type(error).__name__)

    def adopt(self, handle: str, incoming: Path, *, keep: Path | None = None) -> Path | None:
        """Validate a finished download and move it into the cache."""
        try:
            data = self._read_private(incoming)
        except OSError as error:
            log.info("discarding cover image: %s", type(error).__name__)
            data = None
        if data is None or jpeg_dimensions(data) is None:
            if data is not None:
                log.info("discarding cover image that is not a valid JPEG")
            self.discard(incoming)
            return None
        final = incoming.with_name(f"{_FILE_PREFIX}{secrets.token_hex(8)}-{handle}.jpg")
        try:
            os.rename(incoming, final)
        except OSError as error:
            log.info("could not store cover image: %s", type(error).__name__)
            self.discard(incoming)
            return None
        old = self._entries.pop(handle, None)
        if old is not None and old[0] != keep:
            self.discard(old[0])
        self._entries[handle] = (final, len(data))
        self._evict(keep=keep)
        return final

    def _read_private(self, path: Path) -> bytes | None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid():
                return None
            if current.st_size > MAX_IMAGE_BYTES:
                log.info("discarding cover image larger than %d bytes", MAX_IMAGE_BYTES)
                return None
            # obexd wrote it; make sure it is owner-only before it is shared.
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            chunks = []
            remaining = MAX_IMAGE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return data
        finally:
            os.close(descriptor)

    def _evict(self, *, keep: Path | None) -> None:
        def over() -> bool:
            total = sum(size for _path, size in self._entries.values())
            return len(self._entries) > self._max_files or total > self._max_bytes

        for handle in list(self._entries):
            if not over():
                break
            path, _size = self._entries[handle]
            if path == keep:
                continue
            del self._entries[handle]
            self.discard(path)

    def clear(self) -> None:
        """Remove every cached and half-written image, also from a crash."""
        self._entries.clear()
        directory = self.directory
        try:
            names = list(directory.iterdir())
        except FileNotFoundError:
            return
        except OSError as error:
            log.debug("could not list cover cache: %s", type(error).__name__)
            return
        for path in names:
            if not path.name.startswith((_FILE_PREFIX, _INCOMING_PREFIX)):
                continue
            try:
                current = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid():
                self.discard(path)


class ObexConnection(Protocol):
    """A private session-bus connection that owns the BIP session."""

    @property
    def bus(self) -> GioBus: ...

    def close(self) -> None: ...


ObexConnector = Callable[
    [
        Callable[[ObexConnection], None],      # ready
        Callable[[Exception], None],           # failed
        Callable[[ObexConnection], None],      # closed by the bus
    ],
    None,
]


class _GioObexConnection:
    def __init__(self, connection: Gio.DBusConnection) -> None:
        self._connection = connection
        self.bus: GioBus = GioDBus(lambda: connection)

    def close(self) -> None:
        if not self._connection.is_closed():
            # Asynchronous; obexd ends the session when this client leaves.
            self._connection.close(None, None)


def connect_private_session_bus(
    ready: Callable[[ObexConnection], None],
    failed: Callable[[Exception], None],
    closed: Callable[[ObexConnection], None],
) -> None:
    """Open a private session-bus connection without blocking the loop."""
    try:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        failed(call_error(error))
        return
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )

    def finished(_source: object, result: Gio.AsyncResult) -> None:
        try:
            connection = Gio.DBusConnection.new_for_address_finish(result)
        except GLib.Error as error:
            failed(call_error(error))
            return
        connection.set_exit_on_close(False)
        wrapper = _GioObexConnection(connection)
        connection.connect("closed", lambda *_args: closed(wrapper))
        ready(wrapper)

    Gio.DBusConnection.new_for_address(address, flags, None, None, finished)


@dataclass(slots=True)
class _Player:
    port: int | None = None
    track: dict[str, Any] = field(default_factory=dict)
    browsable: bool = False
    playlist: str | None = None


@dataclass(slots=True)
class _Fetch:
    handle: str
    incoming: Path
    transfer: str | None = None
    timeout_id: int | None = None


class PhoneCoverArt:
    """Keep the phone's BIP session and publish the current cover file."""

    def __init__(
        self,
        system_bus: GioBus,
        device_path: str,
        mac: str,
        *,
        on_cover: Callable[[str | None], None],
        schedule: Schedule,
        cancel: Cancel,
        connect_obex: ObexConnector = connect_private_session_bus,
        cache: CoverCache | None = None,
        enabled: bool = False,
    ) -> None:
        self._bus = system_bus
        self._device_path = device_path
        self._mac = mac
        self._on_cover = on_cover
        self._schedule = schedule
        self._cancel = cancel
        self._connect = connect_obex
        self.cache = cache if cache is not None else CoverCache()
        self._enabled = enabled
        self._started = False
        self._stopped = False
        self._owner_available = True
        self._matches: list[Subscription] = []
        self._players: dict[str, _Player] = {}
        self._active_player: str | None = None
        self._avrcp_connected: bool | None = None
        self._probe_generation = 0
        self._watch_retry_id: int | None = None
        self._grace_id: int | None = None
        # BIP session state.
        self._generation = 0
        self._conn: ObexConnection | None = None
        self._conn_matches: list[Subscription] = []
        self._opening = False
        self._session_path: str | None = None
        self._session_port: int | None = None
        self._retry_id: int | None = None
        self._failures = 0
        self._early_status: dict[str, str] = {}
        self._fetch: _Fetch | None = None
        self._listing = False
        self._listed = False
        self._failed_handles: set[str] = set()
        # Image handles of Now Playing items learned through browsing.
        self._item_handles: dict[str, str] = {}
        self._cover_handle: str | None = None
        self.path: str | None = None

    # ---- public --------------------------------------------------------

    @property
    def session_open(self) -> bool:
        return self._session_path is not None

    def snapshot(self) -> dict[str, object]:
        return {"phone_media_cover": self.path or ""}

    def start(self) -> None:
        self._started = True
        self._stopped = False
        # Files of an earlier run are meaningless: handles are per session.
        self.cache.clear()
        if self._enabled:
            self._watch()

    def stop(self) -> None:
        self._stopped = True
        self._unwatch()
        self._close_session()
        self._cancel_retry()
        self.cache.clear()

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not self._started or self._stopped:
            return
        if enabled:
            self._watch()
        else:
            self._unwatch()
            self._close_session()
            self._cancel_retry()
            self.cache.clear()

    def bluez_owner_changed(self, available: bool) -> None:
        self._owner_available = available
        self._probe_generation += 1
        self._players.clear()
        self._active_player = None
        self._avrcp_connected = None
        self._reconcile()
        if available and self._matches:
            self._probe()

    # ---- BlueZ observation ---------------------------------------------

    def _watch(self) -> None:
        if self._matches or self._stopped:
            return
        bus = self._bus
        try:
            self._matches.append(bus.subscribe(
                BLUEZ, OBJECT_MANAGER_IFACE, "InterfacesAdded", self._interfaces_added))
            self._matches.append(bus.subscribe(
                BLUEZ, OBJECT_MANAGER_IFACE, "InterfacesRemoved", self._interfaces_removed))
            self._matches.append(bus.subscribe(
                BLUEZ, PROPERTIES_IFACE, "PropertiesChanged", self._player_changed,
                arg0=PLAYER_IFACE, with_path=True,
            ))
            self._matches.append(bus.subscribe(
                BLUEZ, PROPERTIES_IFACE, "PropertiesChanged", self._control_changed,
                path=self._device_path, arg0=CONTROL_IFACE,
            ))
        except Exception as error:
            log.warning(
                "could not watch the iPhone's media players for cover art, "
                "retrying in %ds: %s", WATCH_RETRY_SEC, error_name(error),
            )
            self._remove(self._matches)
            if self._watch_retry_id is None:
                self._watch_retry_id = self._schedule(WATCH_RETRY_SEC, self._retry_watch)
            return
        self._probe()

    def _retry_watch(self) -> bool:
        self._watch_retry_id = None
        if self._enabled and not self._stopped:
            self._watch()
        return False

    def _unwatch(self) -> None:
        if self._watch_retry_id is not None:
            self._cancel(self._watch_retry_id)
            self._watch_retry_id = None
        self._remove(self._matches)
        self._probe_generation += 1
        self._players.clear()
        self._active_player = None
        self._avrcp_connected = None

    @staticmethod
    def _remove(matches: list[Subscription]) -> None:
        for match in matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove a cover-art watch", exc_info=True)
        matches.clear()

    def _probe(self) -> None:
        if not self._owner_available or self._stopped:
            return
        self._probe_generation += 1
        generation = self._probe_generation

        def reply(objects: Mapping) -> None:
            if generation != self._probe_generation:
                return
            self._players.clear()
            prefix = self._device_path + "/"
            for path, interfaces in objects.items():
                if str(path).startswith(prefix) and PLAYER_IFACE in interfaces:
                    self._players[str(path)] = self._apply_player(
                        _Player(), interfaces[PLAYER_IFACE])
            control = objects.get(self._device_path, {}).get(CONTROL_IFACE)
            if control is not None:
                self._apply_control(control)
            self._reconcile()

        def failed(error: Exception) -> None:
            if generation == self._probe_generation:
                log.debug("could not read the iPhone's media players: %s", error_name(error))

        try:
            self._bus.call(
                BLUEZ, "/", OBJECT_MANAGER_IFACE, "GetManagedObjects", "", (),
                MANAGED_OBJECTS_REPLY, reply, failed, timeout=5.0,
            )
        except Exception as error:
            failed(error)

    @staticmethod
    def _apply_player(player: _Player, properties: Mapping) -> _Player:
        if "ObexPort" in properties:
            try:
                port = int(properties["ObexPort"])
            except (TypeError, ValueError):
                port = 0
            player.port = port if 0 < port <= 0xFFFF else None
        if "Track" in properties:
            track = properties["Track"]
            player.track = dict(track) if isinstance(track, Mapping) else {}
        if "Browsable" in properties:
            player.browsable = bool(properties["Browsable"])
        if "Playlist" in properties:
            player.playlist = str(properties["Playlist"]) or None
        return player

    def _apply_control(self, properties: Mapping) -> None:
        if "Connected" in properties:
            self._avrcp_connected = bool(properties["Connected"])
        if "Player" in properties:
            self._active_player = str(properties["Player"]) or None

    def _interfaces_added(self, path, interfaces) -> None:
        path = str(path)
        if not self._owner_available or self._stopped:
            return
        if path.startswith(self._device_path + "/") and PLAYER_IFACE in interfaces:
            self._players[path] = self._apply_player(_Player(), interfaces[PLAYER_IFACE])
            self._reconcile()
        elif path == self._device_path and CONTROL_IFACE in interfaces:
            self._apply_control(interfaces[CONTROL_IFACE])
            self._reconcile()

    def _interfaces_removed(self, path, interfaces) -> None:
        path = str(path)
        if PLAYER_IFACE in interfaces and self._players.pop(path, None) is not None:
            if self._active_player == path:
                self._active_player = None
            self._reconcile()
        elif path == self._device_path and (
            CONTROL_IFACE in interfaces or DEVICE_IFACE in interfaces
        ):
            self._players.clear()
            self._active_player = None
            self._avrcp_connected = None
            self._reconcile()

    def _player_changed(self, path, interface, changed, _invalidated) -> None:
        player = self._players.get(str(path))
        if player is None or str(interface) != PLAYER_IFACE:
            return
        self._apply_player(player, changed)
        self._reconcile()

    def _control_changed(self, interface, changed, _invalidated) -> None:
        if str(interface) != CONTROL_IFACE:
            return
        self._apply_control(changed)
        self._reconcile()

    def _current_player(self) -> tuple[str, _Player] | None:
        if self._active_player in self._players:
            path = self._active_player
            return path, self._players[path]
        for path in sorted(self._players):
            if self._players[path].port is not None:
                return path, self._players[path]
        return None

    def _wanted_port(self) -> int | None:
        if (
            not self._enabled or self._stopped or not self._owner_available
            or self._avrcp_connected is False
        ):
            return None
        current = self._current_player()
        if current is not None and current[1].port is not None:
            return current[1].port
        ports = [player.port for _path, player in sorted(self._players.items()) if player.port]
        return ports[0] if ports else None

    # ---- BIP session -----------------------------------------------------

    def _player_may_return(self) -> bool:
        """AVRCP is still up, so a vanished player is likely recreated."""
        return (
            self._enabled and not self._stopped and self._owner_available
            and self._avrcp_connected is True
        )

    def _reconcile(self) -> None:
        port = self._wanted_port()
        if port is None:
            if self._player_may_return() and (self._conn is not None or self._opening):
                # Keep the session and the current cover for a moment.
                if self._grace_id is None:
                    self._grace_id = self._schedule(PLAYER_GRACE_SEC, self._grace_expired)
                return
            self._cancel_retry()
            self._failures = 0
            self._close_session()
            return
        self._cancel_grace()
        if self._session_port != port and (self._conn is not None or self._opening):
            log.info("iPhone cover art port changed; reopening the session")
            self._close_session()
        if self._conn is None and not self._opening:
            if self._retry_id is None:
                self._open(port)
            return
        self._update_cover()

    def _grace_expired(self) -> bool:
        self._grace_id = None
        if self._wanted_port() is None:
            log.info("iPhone media player did not come back; closing cover art session")
            self._cancel_retry()
            self._failures = 0
            self._close_session()
        return False

    def _cancel_grace(self) -> None:
        if self._grace_id is not None:
            self._cancel(self._grace_id)
            self._grace_id = None

    def _open(self, port: int) -> None:
        self._generation += 1
        generation = self._generation
        self._opening = True
        self._session_port = port

        def connected(conn: ObexConnection) -> None:
            if generation != self._generation:
                conn.close()
                return
            self._conn = conn
            try:
                self._conn_matches.append(conn.bus.subscribe(
                    OBEX, OBJECT_MANAGER_IFACE, "InterfacesRemoved", self._obex_removed))
                self._conn_matches.append(conn.bus.subscribe(
                    DBUS_NAME, DBUS_NAME, "NameOwnerChanged", self._obex_owner_changed,
                    arg0=OBEX,
                ))
                self._conn_matches.append(conn.bus.subscribe(
                    OBEX, PROPERTIES_IFACE, "PropertiesChanged", self._transfer_changed,
                    arg0=TRANSFER_IFACE, with_path=True,
                ))
                options = {
                    "Target": GLib.Variant("s", BIP_TARGET),
                    "PSM": GLib.Variant("q", port),
                }
                conn.bus.call(
                    OBEX, OBEX_ROOT, CLIENT_IFACE, "CreateSession", "sa{sv}",
                    (self._mac, options), "(o)", created, failed,
                    timeout=CREATE_TIMEOUT_SEC,
                )
            except Exception as error:
                failed(error)

        def created(path) -> None:
            if generation != self._generation:
                return
            self._opening = False
            self._session_path = str(path)
            self._failures = 0
            log.info("iPhone cover art session open (PSM %d)", port)
            self._update_cover()

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            log.info("could not open the iPhone cover art session: %s", _describe(error))
            self._close_session()
            self._schedule_retry()

        def closed(conn: ObexConnection) -> None:
            if conn is self._conn:
                self._session_lost("session bus connection closed")

        try:
            self._connect(connected, failed, closed)
        except Exception as error:
            failed(error)

    def _schedule_retry(self) -> None:
        if self._retry_id is not None or self._stopped:
            return
        delay = RETRY_DELAYS_SEC[min(self._failures, len(RETRY_DELAYS_SEC) - 1)]
        self._failures += 1

        def retry() -> bool:
            self._retry_id = None
            self._reconcile()
            return False

        self._retry_id = self._schedule(delay, retry)

    def _cancel_retry(self) -> None:
        if self._retry_id is not None:
            self._cancel(self._retry_id)
            self._retry_id = None

    def _session_lost(self, reason: str) -> None:
        log.info("iPhone cover art session lost: %s", reason)
        self._close_session()
        self._schedule_retry()

    def _obex_removed(self, path, _interfaces) -> None:
        if self._session_path is not None and str(path) == self._session_path:
            self._session_lost("session disappeared")

    def _obex_owner_changed(self, _name, old_owner, _new_owner) -> None:
        if old_owner and (self._conn is not None):
            self._session_lost("obexd exited")

    def _close_session(self) -> None:
        """Forget the session; closing the owner connection ends it in obexd."""
        had_session = self._conn is not None or self._opening
        self._cancel_grace()
        self._generation += 1
        self._cancel_fetch()
        self._remove(self._conn_matches)
        conn, self._conn = self._conn, None
        self._opening = False
        self._session_path = None
        self._session_port = None
        self._listing = False
        self._listed = False
        self._failed_handles.clear()
        self._item_handles.clear()
        self._early_status.clear()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                log.debug("could not close the cover art connection", exc_info=True)
        if had_session:
            self.cache.clear()
        self._show(None, None)

    # ---- cover selection -------------------------------------------------

    def _current_handle(self) -> str | None:
        current = self._current_player()
        if current is None:
            return None
        track = current[1].track
        handle = valid_image_handle(track.get("ImgHandle"))
        if handle is not None:
            return handle
        item = track.get("Item")
        if item is not None:
            return self._item_handles.get(str(item))
        return None

    def _update_cover(self) -> None:
        if self._session_path is None:
            self._show(None, None)
            return
        handle = self._current_handle()
        if handle is None:
            self._show(None, None)
            self._maybe_list_now_playing()
            return
        if handle == self._cover_handle:
            return
        cached = self.cache.lookup(handle)
        if cached is not None:
            self._show(handle, cached)
            return
        # Never leave the previous track's cover on the new track.
        self._show(None, None)
        if handle not in self._failed_handles:
            self._start_fetch(handle)

    def _show(self, handle: str | None, path: Path | None) -> None:
        text = str(path) if path is not None else None
        self._cover_handle = handle if path is not None else None
        if text == self.path:
            return
        self.path = text
        try:
            self._on_cover(text)
        except Exception:
            log.exception("cover art listener failed")

    def _maybe_list_now_playing(self) -> None:
        """Ask for the current track's attributes again (see module doc)."""
        current = self._current_player()
        if current is None or self._listing:
            return
        path, player = current
        # ListItems makes bluetoothd send SetBrowsedPlayer (and possibly
        # SetAddressedPlayer) for this player; never for a fallback player,
        # only for the one BlueZ already addresses.
        if path != self._active_player:
            return
        item = player.track.get("Item")
        if self._listed or not player.browsable or not player.playlist or item is None:
            return
        # Only the track playing when the session opened lacks its handle.
        self._listed = True
        self._listing = True
        generation = self._generation

        def listed(items) -> None:
            if generation != self._generation:
                return
            self._listing = False
            for item_path, properties in dict(items).items():
                metadata = properties.get("Metadata") if isinstance(properties, Mapping) else None
                handle = valid_image_handle(
                    metadata.get("ImgHandle") if isinstance(metadata, Mapping) else None)
                if handle is not None:
                    self._item_handles[str(item_path)] = handle
            self._update_cover()

        def changed_folder() -> None:
            if generation != self._generation:
                return
            filters = {
                "Start": GLib.Variant("u", 0),
                "End": GLib.Variant("u", NOW_PLAYING_ITEMS - 1),
            }
            self._bus.call(
                BLUEZ, path, FOLDER_IFACE, "ListItems", "a{sv}", (filters,),
                "(a{oa{sv}})", listed, failed, timeout=CALL_TIMEOUT_SEC,
            )

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            self._listing = False
            log.debug("could not read the Now Playing list: %s", error_name(error))

        try:
            self._bus.call(
                BLUEZ, path, FOLDER_IFACE, "ChangeFolder", "o", (player.playlist,),
                "()", changed_folder, failed, timeout=CALL_TIMEOUT_SEC,
            )
        except Exception as error:
            failed(error)

    # ---- thumbnail download -----------------------------------------------

    def _start_fetch(self, handle: str) -> None:
        if self._fetch is not None:
            # Finish the running transfer; _fetch_done picks the latest track.
            return
        conn, session = self._conn, self._session_path
        if conn is None or session is None:
            return
        try:
            incoming = self.cache.reserve(handle)
        except OSError as error:
            log.warning("cover art cache is unavailable: %s", type(error).__name__)
            return
        fetch = _Fetch(handle, incoming)
        self._fetch = fetch
        fetch.timeout_id = self._schedule(
            TRANSFER_TIMEOUT_SEC, lambda: self._fetch_done(fetch, "timeout") and False)

        def started(transfer, properties) -> None:
            if self._fetch is not fetch:
                return
            fetch.transfer = str(transfer)
            status = self._early_status.pop(fetch.transfer, None)
            if status is None and isinstance(properties, Mapping):
                status = properties.get("Status")
            if status in ("complete", "error"):
                self._fetch_done(fetch, str(status))

        def failed(error: Exception) -> None:
            if self._fetch is fetch:
                log.info("cover art request failed: %s", _describe(error))
                self._fetch_done(fetch, "error")

        try:
            conn.bus.call(
                OBEX, session, IMAGE_IFACE, "GetThumbnail", "ss", (str(incoming), handle),
                "(oa{sv})", started, failed, timeout=CALL_TIMEOUT_SEC,
            )
        except Exception as error:
            failed(error)

    def _transfer_changed(self, path, interface, changed, _invalidated) -> None:
        if str(interface) != TRANSFER_IFACE or "Status" not in changed:
            return
        status = str(changed["Status"])
        if status not in ("complete", "error"):
            return
        fetch = self._fetch
        if fetch is None:
            return
        if fetch.transfer is None:
            # The reply naming this transfer is still queued behind us.
            # Only transfers of our own session, so the map stays bounded.
            session = self._session_path
            if session is not None and str(path).startswith(session + "/"):
                self._early_status[str(path)] = status
            return
        if str(path) == fetch.transfer:
            self._fetch_done(fetch, status)

    def _fetch_done(self, fetch: _Fetch, status: str) -> bool:
        if self._fetch is not fetch:
            return False
        self._fetch = None
        if fetch.timeout_id is not None and status != "timeout":
            self._cancel(fetch.timeout_id)
        fetch.timeout_id = None
        stored: Path | None = None
        if status == "complete":
            stored = self.cache.adopt(
                fetch.handle, fetch.incoming,
                keep=Path(self.path) if self.path else None,
            )
        else:
            if status == "timeout":
                log.info("cover art transfer timed out")
            self.cache.discard(fetch.incoming)
        if stored is None:
            # Not again for every Position update; a new session retries.
            self._failed_handles.add(fetch.handle)
        if stored is not None and self._current_handle() == fetch.handle:
            self._show(fetch.handle, stored)
        elif self._current_handle() != fetch.handle:
            self._update_cover()
        return False

    def _cancel_fetch(self) -> None:
        fetch, self._fetch = self._fetch, None
        if fetch is None:
            return
        if fetch.timeout_id is not None:
            self._cancel(fetch.timeout_id)
        self.cache.discard(fetch.incoming)


def _describe(error: Exception) -> str:
    name = error_name(error)
    message = getattr(error, "message", "") or ""
    # "Connection refused (111)": another BIP client holds the phone's slot.
    if "(111)" in message or "refused" in message.lower():
        return f"{name} (another cover art session is open?)"
    return name
