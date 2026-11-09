"""Hermetic tests for AVRCP cover art over BIP (fake BlueZ, fake obexd, fake timers)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from gi.repository import GLib

from blueferry.cover_art import (
    MAX_IMAGE_BYTES,
    CoverCache,
    PhoneCoverArt,
    jpeg_dimensions,
    valid_image_handle,
)
from blueferry.gio_dbus import DBusCallError

DEV = "/org/bluez/hci0/dev_AA"
PLAYER = f"{DEV}/player0"
PLAYER2 = f"{DEV}/player1"
MAC = "AA:BB:CC:DD:EE:FF"


def jpeg(width: int = 200, height: int = 200, *, end: bool = True) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    body = b"\xff\xd8" + app0 + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12" * 64
    return body + (b"\xff\xd9" if end else b"")


class Timers:
    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, object]] = {}
        self._next = 0

    def schedule(self, seconds, callback) -> int:
        self._next += 1
        self.pending[self._next] = (seconds, callback)
        return self._next

    def cancel(self, source_id) -> None:
        self.pending.pop(source_id, None)

    def delays(self) -> list[int]:
        return [seconds for seconds, _ in self.pending.values()]

    def fire(self) -> None:
        for source_id, (_seconds, callback) in list(self.pending.items()):
            self.pending.pop(source_id, None)
            callback()


class Match:
    def __init__(self, registry, key) -> None:
        self._registry = registry
        self._key = key
        self.removed = False

    def remove(self) -> None:
        self.removed = True
        self._registry.pop(self._key, None)


class SystemBus:
    """Fake bluetoothd: ObjectManager tree, signals and browsing calls."""

    def __init__(self, tree=None) -> None:
        self.tree = tree if tree is not None else {}
        self.receivers: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.list_items: dict = {}
        self.list_error: Exception | None = None

    def call(self, bus_name, path, interface, method, signature, args,
             reply_type, on_reply, on_error, *, timeout):
        GLib.Variant(f"({signature})", args)
        assert GLib.VariantType.string_is_valid(reply_type)
        assert bus_name == "org.bluez"
        self.calls.append((path, interface, method, args))
        if method == "GetManagedObjects":
            on_reply(self.tree)
        elif method == "ChangeFolder":
            on_reply()
        elif method == "ListItems":
            if self.list_error is not None:
                on_error(self.list_error)
            else:
                on_reply(self.list_items)
        else:  # pragma: no cover - unexpected
            raise AssertionError(method)

    def subscribe(self, sender, interface, member, handler, *, path=None, arg0=None,
                  with_path=False):
        key = f"{member}:{arg0}" if arg0 else member
        self.receivers[key] = handler
        return Match(self.receivers, key)

    # Signal helpers
    def player_changed(self, path, **changed) -> None:
        self.receivers["PropertiesChanged:org.bluez.MediaPlayer1"](
            path, "org.bluez.MediaPlayer1", changed, [])

    def control_changed(self, **changed) -> None:
        self.receivers["PropertiesChanged:org.bluez.MediaControl1"](
            "org.bluez.MediaControl1", changed, [])

    def added(self, path, interfaces) -> None:
        self.receivers["InterfacesAdded"](path, interfaces)

    def removed(self, path, interfaces) -> None:
        self.receivers["InterfacesRemoved"](path, interfaces)


class Obexd:
    """Fake obexd on private connections; writes thumbnails like obexd does."""

    def __init__(self) -> None:
        self.connections: list[ObexConn] = []
        self.create_errors: list[Exception] = []
        self.images: dict[str, bytes] = {}
        self.auto_complete = True
        self.sessions = 0
        self.thumbnail_calls: list[tuple[str, str]] = []
        self.connect_error: Exception | None = None
        self.hold_connect = False
        self.held: list = []

    def connect(self, ready, failed, closed) -> None:
        if self.connect_error is not None:
            failed(self.connect_error)
            return
        conn = ObexConn(self, closed)
        self.connections.append(conn)
        if self.hold_connect:
            self.held.append((ready, conn))
            return
        ready(conn)

    @property
    def live(self) -> list[ObexConn]:
        return [conn for conn in self.connections if not conn.closed]


class ObexConn:
    def __init__(self, obexd: Obexd, closed_cb) -> None:
        self.obexd = obexd
        self.closed = False
        self.closed_cb = closed_cb
        self.receivers: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.session: str | None = None
        self.transfers = 0
        self.bus = self

    def close(self) -> None:
        self.closed = True

    def call(self, bus_name, path, interface, method, signature, args,
             reply_type, on_reply, on_error, *, timeout):
        GLib.Variant(f"({signature})", args)
        assert bus_name == "org.bluez.obex"
        self.calls.append((path, interface, method, args))
        if method == "CreateSession":
            mac, options = args
            assert mac == MAC
            assert options["Target"].unpack() == "bip-avrcp"
            assert options["PSM"].get_type_string() == "q"
            if self.obexd.create_errors:
                on_error(self.obexd.create_errors.pop(0))
                return
            self.obexd.sessions += 1
            self.session = f"/org/bluez/obex/client/session{self.obexd.sessions}"
            on_reply(self.session)
        elif method == "GetThumbnail":
            assert path == self.session
            target, handle = args
            self.obexd.thumbnail_calls.append((target, handle))
            self.transfers += 1
            transfer = f"{self.session}/transfer{self.transfers}"
            on_reply(transfer, {"Status": "queued"})
            if self.obexd.auto_complete:
                self.finish(transfer, target, handle)
        else:  # pragma: no cover
            raise AssertionError(method)

    def finish(self, transfer, target, handle, status="complete") -> None:
        if status == "complete":
            Path(target).write_bytes(self.obexd.images.get(handle, jpeg()))
        self.receivers["PropertiesChanged:org.bluez.obex.Transfer1"](
            transfer, "org.bluez.obex.Transfer1", {"Status": status}, [])

    def subscribe(self, sender, interface, member, handler, *, path=None, arg0=None,
                  with_path=False):
        key = f"{member}:{arg0}" if arg0 else member
        self.receivers[key] = handler
        return Match(self.receivers, key)


def tree(*, port=4105, track=None, browsable=False, connected=True, player=PLAYER):
    props = {"Track": track or {"Title": "Song"}, "Browsable": browsable}
    if port is not None:
        props["ObexPort"] = port
    if browsable:
        props["Playlist"] = f"{player}/NowPlaying"
    return {
        DEV: {
            "org.bluez.Device1": {"Connected": True},
            "org.bluez.MediaControl1": {"Connected": connected, "Player": player},
        },
        player: {"org.bluez.MediaPlayer1": props},
    }


@pytest.fixture
def env(tmp_path):
    class Env:
        pass

    e = Env()
    e.bus = SystemBus(tree())
    e.obexd = Obexd()
    e.timers = Timers()
    e.covers: list = []
    e.cache = CoverCache(tmp_path / "cover")

    def make(enabled=True):
        e.art = PhoneCoverArt(
            e.bus, DEV, MAC,
            on_cover=e.covers.append,
            schedule=e.timers.schedule, cancel=e.timers.cancel,
            connect_obex=e.obexd.connect, cache=e.cache, enabled=enabled,
        )
        return e.art

    e.make = make
    return e


# ---- validation ------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("1000039", "1000039"),
    ("0000001", "0000001"),
    ("123456", None),
    ("12345678", None),
    ("12345a7", None),
    ("\uff11\uff12\uff13\uff14\uff15\uff16\uff17", None),  # fullwidth digits
    ("../../x", None),
    ("1234567\n", None),
    (1234567, None),
    (None, None),
    ("", None),
])
def test_image_handle_validation(value, expected):
    assert valid_image_handle(value) == expected


def test_jpeg_dimensions_accepts_real_structure():
    assert jpeg_dimensions(jpeg(200, 200)) == (200, 200)
    assert jpeg_dimensions(jpeg(640, 480)) == (640, 480)
    assert jpeg_dimensions(jpeg(1024, 1024)) == (1024, 1024)


@pytest.mark.parametrize("data", [
    b"",
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
    jpeg(end=False),           # truncated transfer
    jpeg(0, 200),
    jpeg(5000, 200),           # decoder bomb guard
    jpeg(1025, 200),
    b"\xff\xd8\xff\xda\x00\x08" + b"\x00" * 10 + b"\xff\xd9",  # scan before frame
    b"\xff\xd8\xff\xe0\x00\x01\xff\xd9",                      # bad length
])
def test_jpeg_dimensions_rejects(data):
    assert jpeg_dimensions(data) is None


# ---- cache -----------------------------------------------------------------

def test_cache_is_private_and_validates(tmp_path):
    cache = CoverCache(tmp_path / "c")
    incoming = cache.reserve("1000001")
    assert stat.S_IMODE((tmp_path / "c").stat().st_mode) == 0o700
    assert stat.S_IMODE(incoming.stat().st_mode) == 0o600
    incoming.write_bytes(jpeg())
    stored = cache.adopt("1000001", incoming)
    assert stored is not None and stored.name.startswith("cover-")
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert not incoming.exists()
    assert cache.lookup("1000001") == stored

    bad = cache.reserve("1000002")
    bad.write_bytes(b"not an image")
    assert cache.adopt("1000002", bad) is None
    assert not bad.exists()


def test_cache_rejects_oversized_and_symlinks(tmp_path):
    cache = CoverCache(tmp_path / "c")
    big = cache.reserve("1000003")
    big.write_bytes(jpeg()[:-2] + b"\x00" * (MAX_IMAGE_BYTES + 1) + b"\xff\xd9")
    assert cache.adopt("1000003", big) is None
    assert not big.exists()

    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(jpeg())
    link = (tmp_path / "c") / ".incoming-link-1000004.jpg"
    link.symlink_to(target)
    assert cache.adopt("1000004", link) is None
    assert target.exists()


def test_cache_evicts_oldest_but_keeps_current(tmp_path):
    cache = CoverCache(tmp_path / "c", max_files=3)
    stored = []
    for index in range(3):
        incoming = cache.reserve(f"100000{index}")
        incoming.write_bytes(jpeg())
        stored.append(cache.adopt(f"100000{index}", incoming, keep=stored[0] if stored else None))
    incoming = cache.reserve("1000009")
    incoming.write_bytes(jpeg())
    cache.adopt("1000009", incoming, keep=stored[0])
    assert len(cache) == 3
    assert stored[0].exists()          # displayed, protected
    assert not stored[1].exists()      # oldest unprotected
    assert cache.lookup("1000001") is None


def test_cache_byte_bound(tmp_path):
    size = len(jpeg())
    cache = CoverCache(tmp_path / "c", max_bytes=size * 2)
    for index in range(4):
        incoming = cache.reserve(f"200000{index}")
        incoming.write_bytes(jpeg())
        cache.adopt(f"200000{index}", incoming)
    assert len(cache) == 2


def test_cache_clear_removes_leftovers_only(tmp_path):
    directory = tmp_path / "c"
    directory.mkdir(mode=0o700)
    (directory / "cover-old-1234567.jpg").write_bytes(b"x")
    (directory / ".incoming-old-1234567.jpg").write_bytes(b"x")
    (directory / "unrelated.txt").write_bytes(b"x")
    CoverCache(directory).clear()
    assert sorted(path.name for path in directory.iterdir()) == ["unrelated.txt"]


# ---- session lifecycle ---------------------------------------------------------

def test_opens_one_session_when_obex_port_known(env):
    art = env.make()
    art.start()
    assert len(env.obexd.connections) == 1
    conn = env.obexd.connections[0]
    (_path, iface, method, (_mac, options)) = conn.calls[0]
    assert (iface, method) == ("org.bluez.obex.Client1", "CreateSession")
    assert options["PSM"].unpack() == 4105
    assert art.session_open
    # Further player updates do not open a second session (iPhone allows one).
    env.bus.player_changed(PLAYER, Position=1000)
    env.bus.added(PLAYER2, {"org.bluez.MediaPlayer1": {"ObexPort": 4105, "Track": {}}})
    assert len(env.obexd.connections) == 1


def test_disabled_is_inert(env):
    art = env.make(enabled=False)
    art.start()
    assert env.bus.receivers == {}
    assert env.bus.calls == []
    assert env.obexd.connections == []
    art.set_enabled(True)
    assert art.session_open
    art.set_enabled(False)
    assert env.obexd.live == []
    assert env.bus.receivers == {}
    assert art.path is None


def test_waits_for_obex_port(env):
    env.bus.tree = tree(port=None)
    art = env.make()
    art.start()
    assert env.obexd.connections == []
    env.bus.player_changed(PLAYER, ObexPort=4105)
    assert art.session_open


def test_avrcp_disconnect_closes_session(env):
    art = env.make()
    art.start()
    conn = env.obexd.connections[0]
    env.bus.control_changed(Connected=False)
    assert conn.closed
    assert not art.session_open
    env.bus.control_changed(Connected=True)
    assert art.session_open
    # The device object going away closes at once, without a grace period.
    env.bus.removed(DEV, {"org.bluez.MediaControl1": {}})
    assert env.obexd.live == []
    assert env.timers.pending == {}


def test_port_change_reopens(env):
    art = env.make()
    art.start()
    first = env.obexd.connections[0]
    env.bus.added(PLAYER2, {"org.bluez.MediaPlayer1": {"ObexPort": 4107, "Track": {}}})
    env.bus.control_changed(Player=PLAYER2)
    assert first.closed
    assert art.session_open
    assert env.obexd.live[0].calls[0][3][1]["PSM"].unpack() == 4107


def test_create_failure_backs_off(env):
    refused = DBusCallError("org.bluez.obex.Error.Failed", "Connection refused (111)")
    env.obexd.create_errors = [refused, refused]
    art = env.make()
    art.start()
    assert not art.session_open
    assert env.obexd.live == []
    assert env.timers.delays() == [2]
    env.timers.fire()
    assert env.timers.delays() == [5]
    env.timers.fire()
    assert art.session_open
    assert env.timers.pending == {}


def test_connect_failure_backs_off(env):
    env.obexd.connect_error = DBusCallError("org.freedesktop.DBus.Error.Failed", "no bus")
    art = env.make()
    art.start()
    assert env.timers.delays() == [2]
    env.obexd.connect_error = None
    env.timers.fire()
    assert art.session_open


def test_obexd_restart_and_session_removal_reopen(env):
    art = env.make()
    art.start()
    conn = env.obexd.connections[0]
    conn.receivers["NameOwnerChanged:org.bluez.obex"]("org.bluez.obex", ":1.5", "")
    assert conn.closed and not art.session_open
    env.timers.fire()
    conn = env.obexd.live[0]
    conn.receivers["InterfacesRemoved"](conn.session, ["org.bluez.obex.Session1"])
    assert conn.closed
    env.timers.fire()
    assert art.session_open
    conn = env.obexd.live[0]
    conn.closed_cb(conn)  # the bus dropped the private connection
    assert not art.session_open


def test_late_connection_after_disconnect_is_closed(env):
    env.obexd.hold_connect = True
    art = env.make()
    art.start()
    env.bus.control_changed(Connected=False)
    ready, conn = env.obexd.held.pop()
    ready(conn)
    assert conn.closed
    assert conn.calls == []


def test_stop_closes_session_and_clears_cache(env):
    env.bus.tree = tree(track={"Title": "Song", "ImgHandle": "1000001"})
    art = env.make()
    art.start()
    assert art.path is not None and Path(art.path).exists()
    art.stop()
    assert env.obexd.live == []
    assert env.bus.receivers == {}
    assert not any(env.cache.directory.iterdir())
    assert env.covers[-1] is None


def test_bluez_restart_resets(env):
    art = env.make()
    art.start()
    art.bluez_owner_changed(False)
    assert env.obexd.live == []
    art.bluez_owner_changed(True)
    assert art.session_open


# ---- cover download ------------------------------------------------------------

def test_handle_fetches_validated_thumbnail(env):
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "Next", "ImgHandle": "1000039"})
    target, handle = env.obexd.thumbnail_calls[0]
    assert handle == "1000039"
    assert Path(target).parent == env.cache.directory
    assert art.path is not None
    assert Path(art.path).read_bytes() == jpeg()
    assert stat.S_IMODE(os.stat(art.path).st_mode) == 0o600
    assert art.snapshot() == {"phone_media_cover": art.path}
    assert env.covers == [art.path]


def test_no_handle_removes_cover(env):
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    assert art.path
    env.bus.player_changed(PLAYER, Track={"Title": "B"})
    assert art.path is None
    assert env.covers[-1] is None
    assert art.snapshot() == {"phone_media_cover": ""}


def test_invalid_handle_is_ignored(env):
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "../../etc"})
    assert env.obexd.thumbnail_calls == []
    assert art.path is None


def test_cached_handle_is_reused(env):
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    first = art.path
    env.bus.player_changed(PLAYER, Track={"Title": "B", "ImgHandle": "1000002"})
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    assert art.path == first
    assert [h for _t, h in env.obexd.thumbnail_calls] == ["1000001", "1000002"]


def test_invalid_image_is_dropped_and_not_refetched(env):
    env.obexd.images["1000001"] = b"<html>nope</html>"
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    assert art.path is None
    env.bus.player_changed(PLAYER, Position=5)
    assert len(env.obexd.thumbnail_calls) == 1
    assert [p.name for p in env.cache.directory.iterdir()] == []


def test_transfer_error_and_timeout(env):
    env.obexd.auto_complete = False
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    conn = env.obexd.live[0]
    target, _handle = env.obexd.thumbnail_calls[0]
    conn.finish(f"{conn.session}/transfer1", target, "1000001", status="error")
    assert art.path is None
    assert not Path(target).exists()
    env.bus.player_changed(PLAYER, Track={"Title": "B", "ImgHandle": "1000002"})
    target2, _ = env.obexd.thumbnail_calls[1]
    assert Path(target2).exists()
    env.timers.fire()  # transfer timeout
    assert not Path(target2).exists()
    assert art.path is None


def test_track_change_during_transfer_fetches_latest(env):
    env.obexd.auto_complete = False
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    env.bus.player_changed(PLAYER, Track={"Title": "B", "ImgHandle": "1000002"})
    assert len(env.obexd.thumbnail_calls) == 1  # one transfer at a time
    conn = env.obexd.live[0]
    target, handle = env.obexd.thumbnail_calls[0]
    env.obexd.auto_complete = True
    conn.finish(f"{conn.session}/transfer1", target, handle)
    # A's image is cached but not shown on B; B is fetched next.
    assert [h for _t, h in env.obexd.thumbnail_calls] == ["1000001", "1000002"]
    assert art.path is not None and art.path.endswith("-1000002.jpg")


def test_status_signal_before_reply_is_kept(env):
    """GDBus delivers in order, but a status seen before the reply still counts."""
    art = env.make()
    art.start()
    conn = env.obexd.live[0]
    original = conn.call

    def racing(bus_name, path, interface, method, signature, args, reply_type,
               on_reply, on_error, *, timeout):
        if method != "GetThumbnail":
            return original(bus_name, path, interface, method, signature, args,
                            reply_type, on_reply, on_error, timeout=timeout)
        target, _handle = args
        Path(target).write_bytes(jpeg())
        transfer = f"{conn.session}/transfer9"
        conn.receivers["PropertiesChanged:org.bluez.obex.Transfer1"](
            transfer, "org.bluez.obex.Transfer1", {"Status": "complete"}, [])
        on_reply(transfer, {})

    conn.call = racing
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    assert art.path is not None


def test_session_end_clears_cover_and_cache(env):
    art = env.make()
    art.start()
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    path = Path(art.path)
    env.bus.control_changed(Connected=False)
    assert art.path is None
    assert not path.exists()


def test_previous_run_leftovers_are_swept_on_start(env):
    env.cache.directory.mkdir(parents=True, mode=0o700)
    leftover = env.cache.directory / "cover-dead-1234567.jpg"
    leftover.write_bytes(jpeg())
    env.make(enabled=False).start()
    assert not leftover.exists()


# ---- current track at session open ------------------------------------------

def test_now_playing_list_supplies_handle_of_running_track(env):
    item = f"{PLAYER}/NowPlaying/item42"
    env.bus.tree = tree(browsable=True, track={"Title": "Running", "Item": item})
    env.bus.list_items = {
        f"{PLAYER}/NowPlaying/item41": {"Metadata": {"ImgHandle": "1000040"}},
        item: {"Metadata": {"Title": "Running", "ImgHandle": "1000041"}},
    }
    art = env.make()
    art.start()
    methods = [(c[1], c[2], c[3]) for c in env.bus.calls if c[2] != "GetManagedObjects"]
    assert methods[0] == (
        "org.bluez.MediaFolder1", "ChangeFolder", (f"{PLAYER}/NowPlaying",))
    assert methods[1][1] == "ListItems"
    assert [h for _t, h in env.obexd.thumbnail_calls] == ["1000041"]
    assert art.path is not None
    # Only once per session, not for every later track without a handle.
    env.bus.player_changed(PLAYER, Track={"Title": "Other", "Item": f"{PLAYER}/NowPlaying/x"})
    assert sum(1 for c in env.bus.calls if c[2] == "ListItems") == 1


def test_without_browsing_cover_starts_with_next_track(env):
    env.bus.tree = tree(browsable=False, track={"Title": "Running", "Item": "/x"})
    art = env.make()
    art.start()
    assert not any(c[2] in ("ChangeFolder", "ListItems") for c in env.bus.calls)
    assert art.path is None
    env.bus.player_changed(PLAYER, Track={"Title": "Next", "ImgHandle": "1000002"})
    assert art.path is not None


def test_now_playing_list_failure_is_harmless(env):
    env.bus.tree = tree(browsable=True, track={"Title": "Running", "Item": "/i"})
    env.bus.list_error = DBusCallError("org.bluez.Error.Failed", "Not supported")
    art = env.make()
    art.start()
    assert art.session_open and art.path is None
    env.bus.player_changed(PLAYER, Track={"Title": "Next", "ImgHandle": "1000002"})
    assert art.path is not None


def test_vanished_player_gets_a_grace_period_while_avrcp_is_up(env):
    env.bus.tree = tree(track={"Title": "A", "ImgHandle": "1000001"})
    art = env.make()
    art.start()
    conn = env.obexd.live[0]
    cover = art.path
    assert cover is not None
    # iOS recreates its player on an app change or pause/play.
    env.bus.removed(PLAYER, {"org.bluez.MediaPlayer1": {}})
    assert not conn.closed and art.session_open
    assert art.path == cover          # no flicker
    assert env.timers.delays() == [5]
    env.bus.added(PLAYER2, {"org.bluez.MediaPlayer1": {
        "ObexPort": 4105, "Track": {"Title": "A", "ImgHandle": "1000001"}}})
    env.bus.control_changed(Player=PLAYER2)
    assert env.timers.pending == {}   # grace cancelled
    assert env.obexd.live == [conn]   # same session, no reopen
    assert art.path == cover          # served from the cache
    assert len(env.obexd.thumbnail_calls) == 1


def test_grace_period_expires_and_closes(env):
    art = env.make()
    art.start()
    conn = env.obexd.live[0]
    env.bus.removed(PLAYER, {"org.bluez.MediaPlayer1": {}})
    env.timers.fire()
    assert conn.closed and not art.session_open
    assert env.timers.pending == {}   # no retry without a player


def test_avrcp_disconnect_during_grace_closes_at_once(env):
    art = env.make()
    art.start()
    conn = env.obexd.live[0]
    env.bus.removed(PLAYER, {"org.bluez.MediaPlayer1": {}})
    env.bus.control_changed(Connected=False)
    assert conn.closed
    assert env.timers.pending == {}


def test_now_playing_list_only_for_the_addressed_player(env):
    """ListItems makes bluetoothd switch the browsed/addressed player."""
    item = f"{PLAYER}/NowPlaying/item42"
    env.bus.tree = tree(browsable=True, track={"Title": "Running", "Item": item})
    # BlueZ addresses another (gone) player; ours is only the fallback.
    env.bus.tree[DEV]["org.bluez.MediaControl1"]["Player"] = f"{DEV}/player9"
    art = env.make()
    art.start()
    assert art.session_open
    assert not any(c[2] in ("ChangeFolder", "ListItems") for c in env.bus.calls)


def test_relative_xdg_cache_home_is_ignored(monkeypatch, tmp_path):
    from blueferry.cover_art import cache_directory

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert cache_directory() == tmp_path / ".cache" / "blueferry" / "cover"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert cache_directory() == tmp_path / "c" / "blueferry" / "cover"


def test_early_status_only_for_own_session(env):
    env.obexd.auto_complete = False
    art = env.make()
    art.start()
    conn = env.obexd.live[0]
    original = conn.call
    held = []

    def holding(bus_name, path, interface, method, signature, args, reply_type,
                on_reply, on_error, *, timeout):
        if method == "GetThumbnail":
            held.append(on_reply)
            return None
        return original(bus_name, path, interface, method, signature, args,
                        reply_type, on_reply, on_error, timeout=timeout)

    conn.call = holding
    env.bus.player_changed(PLAYER, Track={"Title": "A", "ImgHandle": "1000001"})
    signal = conn.receivers["PropertiesChanged:org.bluez.obex.Transfer1"]
    signal("/org/bluez/obex/client/session99/transfer1", "org.bluez.obex.Transfer1",
           {"Status": "complete"}, [])
    signal(f"{conn.session}/transfer1", "org.bluez.obex.Transfer1", {"Status": "error"}, [])
    assert list(art._early_status) == [f"{conn.session}/transfer1"]


def test_adopt_makes_the_file_owner_only(tmp_path):
    cache = CoverCache(tmp_path / "c")
    incoming = cache.reserve("1000001")
    incoming.write_bytes(jpeg())
    incoming.chmod(0o644)  # as if obexd had used another mode
    stored = cache.adopt("1000001", incoming)
    assert stored is not None
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
