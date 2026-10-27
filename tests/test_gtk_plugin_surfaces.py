"""GTK "From Plugins", "Send to…", the Tools section and the Photos tab on a
private Broadway display, and CardChanged on the private test bus.

The shared models run on :class:`tests.client_fakes.ManualRunner`\\ s, so a
scenario decides when the "worker" answers.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from gi.repository import Gio, GLib

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize(
    "scenario", ["cards", "send", "tools", "photos", "close", "remembered"],
)
def test_gtk_plugin_surfaces(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


# ---- CardChanged on the private bus (no display) ---------------------------------


def _connect() -> Gio.DBusConnection:
    return Gio.DBusConnection.new_for_address_sync(
        os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None,
    )


def _iterate(seconds: float = 0.3) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)


@pytest.mark.private_dbus
def test_card_changed_is_heard_only_from_the_owner_of_a_watched_name() -> None:
    from blueferry.gio_dbus import GioDBus
    from blueferry.plugin_api import OBJECT_PATH, SIGNAL_CARD_CHANGED, SURFACES_INTERFACE
    from blueferry.ui.card_watch import CardChangeWatcher

    plugin, intruder, listener = _connect(), _connect(), _connect()
    name = "io.example.CardPlugin"
    owned = plugin.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "RequestName", GLib.Variant("(su)", (name, 4)), GLib.VariantType("(u)"),
        Gio.DBusCallFlags.NONE, 5000, None,
    )
    assert owned.unpack() == (1,)
    heard: list[str] = []
    watcher = CardChangeWatcher(lambda: heard.append("changed"), GioDBus(lambda: listener))

    def emit(connection: Gio.DBusConnection) -> None:
        connection.emit_signal(None, OBJECT_PATH, SURFACES_INTERFACE, SIGNAL_CARD_CHANGED, None)
        connection.flush_sync(None)

    try:
        watcher.watch([name])
        assert watcher.names == [name]
        _iterate()
        emit(intruder)  # any other client: ignored
        _iterate()
        assert heard == []
        emit(plugin)
        _iterate()
        assert heard == ["changed"]
        # Disabled meanwhile: no longer watched.
        watcher.watch([])
        emit(plugin)
        _iterate()
        assert heard == ["changed"] and watcher.names == []
        watcher.watch([name])
        watcher.stop()
        emit(plugin)
        _iterate()
        assert heard == ["changed"]
    finally:
        for connection in (plugin, intruder, listener):
            connection.close_sync(None)


# ---- widget scenarios (child process) ------------------------------------------------


class RecordingBus:
    """A GioBus that records subscriptions and lets the scenario signal."""

    def __init__(self):
        self.handlers = {}

    def subscribe(self, sender, interface, member, handler, *, path=None, **_options):
        bus = self

        class Subscription:
            def remove(self):
                bus.handlers.pop(sender, None)

        self.handlers[sender] = (interface, member, path, handler)
        return Subscription()


EXTRA_ITEMS: list = []  # items a scenario adds to the calendar plugin later


def _cards():
    from blueferry import plugin_surfaces as surfaces
    from blueferry.plugin_api.surfaces import Action, CardItem

    return [
        surfaces.PluginCard("io.example.calendar", "<b>Calendar</b>", True, "", [
            CardItem("next", "<i>Dentist</i>", icon="x-office-calendar", subtitle="10:00", actions=[
                Action("open", "Open", kind="primary"), Action("snooze", "Snooze <b>5</b>")]),
            *EXTRA_ITEMS,
        ]),
        surfaces.PluginCard("io.example.ls", "LocalSend", True, "", [
            CardItem("phone", "iPhone", icon="../evil", actions=[
                Action("send", "Send files…", send_to="iphone")]),
        ], can_send=True),
        surfaces.PluginCard("io.example.broken", "Broken", False, "Unavailable: <b>timeout</b>"),
    ]


class FakeUiState:
    """Remembered window state, in memory."""

    def __init__(self, values):
        self.values = dict(values)
        self.saved = []

    def load(self, done):
        done(dict(self.values))

    def save(self, **values):
        self.saved.append(values)
        self.values.update(values)


def _window(saved=None, **window_options):
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry.plugin_surfaces_model import PluginSurfacesModel
    from blueferry.setup_client import ConfigurationState
    from blueferry.ui import window as window_module
    from tests.client_fakes import ManualRunner
    from tests.test_gtk_phone_card import FakeClient, FakeSettings

    Adw, Gtk = window_module.Adw, window_module.Gtk
    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    work, clock = ManualRunner(), ManualRunner()
    invoked, sent, opened, chosen = [], [], [], []
    bus = RecordingBus()

    def surfaces(on_change, opener, watch):
        from blueferry import plugin_surfaces as surfaces

        choice = surfaces.ShareChoice("io.example.ls", "LocalSend", "mac", "MacBook", "computer")

        def card_choice(plugin_id, target, label):
            return surfaces.ShareChoice(plugin_id, "LocalSend", target, label, "")

        model = PluginSurfacesModel(
            runner=work, timer=work.timer, opener=opener,
            on_change=on_change, watch=watch,
            load_cards=_cards, share_available=lambda: True,
            card_names=lambda: ["io.example.Calendar", "io.example.LocalSend"],
            load_targets=lambda: surfaces.ShareTargets([choice], []),
            find=lambda plugin_id, _capability: plugin_id,
            invoke=lambda plugin, item, action: invoked.append((plugin, item, action))
            or surfaces.Outcome(True, "Opened <b>it</b>", "https://cal.example/e"),
            send=lambda target, files: sent.append((target.key, list(files)))
            or surfaces.Outcome(True, "Sending 1 file"),
            card_choice=card_choice,
        )
        return model

    def files(_parent, title, done):
        chosen.append((title, done))

    ui_state = FakeUiState(saved or {})
    configuration = ConfigurationState.from_dict({"configured": True, "saved": True})
    client = FakeClient()
    FakeSettings.opened = []
    with (
        patch.object(window_module, "SetupClient",
                     lambda: SimpleNamespace(configuration=lambda: configuration)),
        patch.object(window_module, "SettingsDialog", FakeSettings),
    ):
        window = window_module.MainWindow(
            None, client, name_loader=lambda done: done("iPhone"),
            surfaces=surfaces, card_bus=bus, files=files, timer=clock.timer,
            # Nothing is ever launched or written: fakes for every way out.
            opener=opened.append, open_file=opened.append, ui_state=ui_state,
            **window_options,
        )
    window.set_default_size(980, 760)
    window.present()
    _settle()
    return SimpleNamespace(window=window, client=client, work=work, invoked=invoked,
                           ui_state=ui_state,
                           sent=sent, opened=opened, chosen=chosen, bus=bus)


def _settle():
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.01)


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _labels(widget):
    from gi.repository import Gtk

    return [w.get_label() for w in _descendants(widget)
            if isinstance(w, Gtk.Label) and w.get_label()]


def _no_markup(widget):
    from gi.repository import Adw, Gtk

    for child in _descendants(widget):
        if isinstance(child, Gtk.Label):
            assert not child.get_use_markup() or not child.get_label(), child.get_label()
        if isinstance(child, Adw.PreferencesRow):
            assert child.get_use_markup() is False, child.get_title()


def _exercise_cards():
    from blueferry.ui.window import Adw, Gtk

    t = _window()
    window, section = t.window, t.window.plugin_cards
    # Folded by default and after the first card load, like the remembered
    # choice says; only a new item later unfolds the panel, without
    # changing that choice.
    from blueferry.plugin_api.surfaces import CardItem

    assert not window.extras.get_reveal_child()
    assert len(t.work.pending) >= 1
    t.work.run_tasks()
    _settle()
    assert not window.extras.get_reveal_child(), "what is there at start is not new"
    section.model.reload()
    t.work.run_tasks()
    assert not window.extras.get_reveal_child(), "the same items again: stays folded"
    EXTRA_ITEMS.append(CardItem("transfer", "Sending 2 files", icon="document-send"))
    section.model.reload()
    t.work.run_tasks()
    _settle()
    assert window.extras.get_reveal_child() and section.get_visible()
    assert t.ui_state.saved == []
    EXTRA_ITEMS.clear()
    section.model.reload()
    t.work.run_tasks()
    window.extras_button.set_active(False)
    assert t.ui_state.saved == [{"plugins_tools_expanded": False}]
    window.extras_button.set_active(True)
    assert t.ui_state.saved[-1] == {"plugins_tools_expanded": True}
    # Only the card plugins' owners may say CardChanged.
    assert sorted(t.bus.handlers) == ["io.example.Calendar", "io.example.LocalSend"]
    interface, member, path, handler = t.bus.handlers["io.example.Calendar"]
    assert (interface, member, path) == (
        "io.weirdware.BlueFerry.Plugin1", "CardChanged", "/io/weirdware/BlueFerry/Plugin")
    labels = _labels(section)
    assert "<b>Calendar</b>" in labels and "LocalSend" in labels  # names: several plugins
    rows = [w for w in _descendants(section.list) if isinstance(w, Adw.ActionRow)]
    titles = [row.get_title() for row in rows]
    assert titles[:3] == ["<i>Dentist</i>", "iPhone", "Broken"]
    assert rows[2].get_subtitle() == "Unavailable: <b>timeout</b>"
    icons = [w.get_icon_name() for w in _descendants(rows[1]) if isinstance(w, Gtk.Image)]
    assert "../evil" not in icons and "application-x-addon-symbolic" in icons
    buttons = {b.get_tooltip_text(): b for b in _descendants(rows[0])
               if isinstance(b, Gtk.Button)}
    assert set(buttons) == {"Snooze <b>5</b>", "Open"}  # raw text, tooltips are plain
    _no_markup(section)

    # A click on the row runs the primary action; the model opens its page.
    rows[0].emit("activated")
    assert section.model.state()["busy"] and not rows[0].get_sensitive()
    t.work.run_tasks()
    assert t.invoked == [("io.example.calendar", "next", "open")]
    assert t.opened == ["https://cal.example/e"]
    assert section.message_row.get_visible()
    assert section.message_row.get_title() == "Opened <b>it</b>"
    section.model.clear_message()
    assert not section.message_row.get_visible()
    buttons["Snooze <b>5</b>"].emit("clicked")
    t.work.run_tasks()
    assert t.invoked[-1] == ("io.example.calendar", "next", "snooze")

    # CardChanged from a watched plugin: one coalesced reload.
    reload_timer = section.model._timer
    t.work.drain()
    handler()
    handler()
    assert reload_timer.active and reload_timer.starts >= 2
    t.work.drain()
    assert not reload_timer.active
    window.destroy()


def _exercise_send():
    import tempfile

    from gi.repository import Gdk

    from blueferry.ui.window import Adw, Gtk

    tmp = Path(tempfile.mkdtemp())
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp / name).write_text(name)
    t = _window()
    window, section = t.window, t.window.plugin_cards
    t.work.run_tasks()
    window.extras_button.set_active(True)
    _settle()
    rows = [w for w in _descendants(section.list) if isinstance(w, Adw.ActionRow)]
    phone = rows[1]
    # An action with sendTo asks for files and sends to that target.
    [send] = [b for b in _descendants(phone) if isinstance(b, Gtk.Button)]
    send.emit("clicked")
    title, done = t.chosen.pop()
    assert title == "Send Files to iPhone"
    assert t.invoked == []
    done([str(tmp / "a.txt")])
    t.work.run_tasks()
    assert [(key, [Path(f).name for f in files]) for key, files in t.sent] == [
        ("io.example.ls:iphone", ["a.txt"])]
    # Files dropped on a device go to the same target; links do not.
    assert set(section.drop_targets) == {"phone"}
    item = section.model.state()["cards"][1]["items"][0]
    links = Gdk.FileList.new_from_list([Gio.File.new_for_uri("https://evil.example/x")])
    assert section._dropped("io.example.ls", item, links) is False
    local = Gdk.FileList.new_from_list([Gio.File.new_for_path(str(tmp / "b.txt"))])
    assert section._dropped("io.example.ls", item, local) is True
    t.work.run_tasks()
    assert [Path(f).name for f in t.sent[-1][1]] == ["b.txt"]

    # "Send to…": asking for targets starts the share plugins, so only on open.
    assert section.send_row.get_visible()
    assert not section.model.state()["targetsLoaded"]
    section.send_popover.emit("show")
    assert section.model.state()["targetsLoading"]
    assert "Looking for targets…" in _labels(section.targets)
    t.work.run_tasks()
    [target] = [w for w in _descendants(section.targets) if isinstance(w, Adw.ActionRow)]
    assert target.get_title() == "MacBook"
    assert section.send_row.get_subtitle() == "MacBook"
    target.emit("activated")
    title, done = t.chosen.pop()
    assert title == "Choose Files to Send"
    done([str(tmp / "c.txt")])
    t.work.run_tasks()
    assert t.sent[-1][0] == "io.example.ls:mac"
    assert [Path(f).name for f in t.sent[-1][1]] == ["c.txt"]
    assert section.message_row.get_title() == "Sending 1 file"
    _no_markup(section)
    window.destroy()


def _exercise_tools():
    import tempfile

    from blueferry.companion_model import CompanionToolsModel
    from blueferry.ui.window import Gtk
    from tests.client_fakes import ManualRunner
    from tests.test_companion_tools import DEVICE, PHOTO_TOOLS, Fake

    fake = Fake(Path(tempfile.mkdtemp()), installed={"uxplay", *PHOTO_TOOLS}, answers=DEVICE)
    tool_work = ManualRunner()

    def tools(on_change):
        return CompanionToolsModel(runner=tool_work, system=fake.system(), on_change=on_change)

    t = _window(tools=tools)
    window, section = t.window, t.window.tools
    poll = section._poll
    window.is_active = lambda: True  # no window manager under Broadway
    window.sync_watches()
    assert not section.watched and not tool_work.pending, "hidden: idevice_id never runs"
    window.extras_button.set_active(True)
    _settle()
    assert section.watched and poll.active and len(tool_work.pending) == 1
    tool_work.run_tasks()
    assert set(section.rows) == {"mirror", "send", "photos"}
    mirror, _spinner, _eject = section.rows["mirror"]
    assert mirror.get_sensitive()
    send, _spinner, _eject = section.rows["send"]
    assert not send.get_sensitive() and send.get_tooltip_text()  # what to install
    photos, spinner, eject = section.rows["photos"]
    assert eject is not None and not eject.get_visible()
    photos.emit("activated")
    assert spinner.get_visible() and not mirror.get_sensitive()
    tool_work.run_tasks()
    assert not spinner.get_visible()
    # Polls while shown; another window in front stops it.
    poll.fire()
    assert poll.active and tool_work.pending
    tool_work.run_tasks()
    window.is_active = lambda: False
    window.sync_watches()
    assert not section.watched and not poll.active
    window.is_active = lambda: True
    window.sync_watches()
    assert section.watched and poll.active
    window.extras_button.set_active(False)
    _settle()
    assert not section.watched and not poll.active
    _no_markup(section)
    assert isinstance(section.list, Gtk.ListBox)
    window.destroy()


def _photo_files(tmp):
    from tests.photo_fixtures import png

    paths = []
    for index in range(3):
        path = tmp / f"thumb{index}.png"
        path.write_bytes(png(8, 8))
        paths.append(path)
    return paths


def _thumbnail_limits(tmp):
    """Oversized files and canvases are never decoded."""
    from gi.repository import GdkPixbuf, Gio

    from blueferry.ui.photos import MAX_THUMBNAIL_BYTES, MAX_THUMBNAIL_PIXELS, load_thumbnail
    from tests.photo_fixtures import png, png_canvas

    (tmp / "small.png").write_bytes(png(8, 8))
    (tmp / "canvas.png").write_bytes(png_canvas(30000, 30000))
    # A real PNG header: only the canvas cap can refuse it, not a parse error.
    info = GdkPixbuf.Pixbuf.get_file_info(str(tmp / "canvas.png"))
    assert info[0] is not None and info[0].get_name() == "png"
    assert (info[1], info[2]) == (30000, 30000) and 30000 * 30000 > MAX_THUMBNAIL_PIXELS
    assert (tmp / "canvas.png").stat().st_size < MAX_THUMBNAIL_BYTES
    (tmp / "large.png").write_bytes(png(8, 8) + b"\0" * (MAX_THUMBNAIL_BYTES + 1))
    results: dict = {}
    for name in ("small", "canvas", "large"):
        uri = Gio.File.new_for_path(str(tmp / f"{name}.png")).get_uri()
        load_thumbnail(uri, Gio.Cancellable(), lambda texture, n=name: results.update({n: texture}))
    deadline = time.monotonic() + 3
    while len(results) < 3:
        assert time.monotonic() < deadline, results
        _settle()
    assert results["small"] is not None
    assert results["canvas"] is None and results["large"] is None


def _exercise_photos():
    import tempfile
    from unittest.mock import patch

    from blueferry import photos_view

    # Never the user's own photo plugin: every plugin call is answered here,
    # from before the tab is first shown.
    answers: dict = {}
    with (
        patch.object(photos_view, "load_current", lambda: answers["list"]),
        patch.object(photos_view, "fetch_current_original",
                     lambda _plugin, _photo: answers["original"]),
    ):
        _photos_scenario(Path(tempfile.mkdtemp()), answers)


def _photos_scenario(tmp, answers):
    from blueferry import photos_view
    from blueferry.plugin_api.client import Photo
    from blueferry.plugin_api.testing import manifest
    from tests.client_fakes import ManualRunner

    thumbs = _photo_files(tmp)
    _thumbnail_limits(tmp)
    list_work, open_work = ManualRunner(), ManualRunner()
    opened = []
    t = _window(photo_runners=(list_work, open_work))
    window, page = t.window, t.window.photos
    page._open_file = opened.append
    plugin = manifest("io.example.photos")
    photos = [
        Photo("a", "2026-10-01T10:00:00", "image", thumbs[0]),
        Photo("b", "2026-10-02T10:00:00", "video", thumbs[1]),
        Photo("c", "", "image", thumbs[2], tmp / "c-original.jpg"),
    ]
    assert not list_work.pending, "nothing is read while the tab is hidden"
    window.show_tab("photos")
    _settle()
    assert page.stack.get_visible_child_name() == "loading"
    answers["list"] = (plugin, photos_view.PhotosSnapshot(
        True, True, "3 recent items from <b>Immich</b>", photos))
    list_work.run_tasks()
    _settle()
    assert page.stack.get_visible_child_name() == "grid"
    assert page.summary.get_label() == "3 recent items from <b>Immich</b>"
    assert list(page.cells) == ["a", "b", "c"]
    deadline = time.monotonic() + 3
    while not all(p.get_paintable() for p in page.pictures.values()):
        assert time.monotonic() < deadline, "thumbnails were not decoded"
        _settle()
    assert page.cells["c"].get_tooltip_text() == \
        "Click to open, or drag the file into another app"
    assert page.cells["a"].get_tooltip_text() == "Click to download and open"
    page.filter_buttons["videos"].set_active(True)
    assert list(page.cells) == ["b"]
    page.filter_buttons["all"].set_active(True)

    # Opening downloads the original (spinner) and opens it.
    page.grid.emit("child-activated", page.cells["a"])
    assert page.download_spinner.get_visible() and open_work.pending
    answers["original"] = tmp / "a.jpg"
    open_work.run_tasks()
    assert not page.download_spinner.get_visible()
    assert opened == [tmp / "a.jpg"]
    assert page.cells["a"].get_tooltip_text() == \
        "Click to open, or drag the file into another app"
    assert page._drag("a") is not None and page._drag("b") is None
    assert not page.loader_banner.get_revealed()

    # A WebP thumbnail without the WebP loader: one banner names it.
    from unittest.mock import patch

    from blueferry.ui import photos as photos_ui
    from tests.test_photo_thumbnails import webp_lossless

    (tmp / "d.webp").write_bytes(webp_lossless(8, 8))
    answers["list"] = (plugin, photos_view.PhotosSnapshot(
        True, True, "4 recent items", [*photos, Photo("d", "", "image", tmp / "d.webp")]))
    with patch.object(photos_ui, "supported_formats", lambda: frozenset({"png", "jpeg"})):
        page.model.refresh()
        list_work.run_tasks()
        deadline = time.monotonic() + 3
        while not page.loader_banner.get_revealed():
            assert time.monotonic() < deadline, "no banner for the missing loader"
            _settle()
    assert "webp-pixbuf-loader" in page.loader_banner.get_title()
    assert page.pictures["d"].get_paintable() is None

    # Hidden: forgotten. Without a plugin, the hint says why (plain text).
    window.show_tab("messages")
    _settle()
    assert page.cells == {}
    answers["list"] = (None, photos_view.PhotosSnapshot(False, False, "Install <b>a plugin</b>"))
    window.show_tab("photos")
    _settle()
    list_work.run_tasks()
    assert page.stack.get_visible_child_name() == "empty"
    assert page.empty_title.get_label() == "No photo plugin"
    assert page.empty_hint.get_label() == "Install <b>a plugin</b>"
    assert not page.refresh_button.get_sensitive() and not page.filters.get_visible()
    _no_markup(page)

    # A plugin was disabled in Settings: the photo plugin is looked up again.
    listener = window.settings.listeners[0]
    listener({"loaded": True, "plugins": [{"id": "io.example.photos", "enabled": True}]})
    assert not list_work.pending, "the first load is not a change"
    listener({"loaded": True, "plugins": [{"id": "io.example.photos", "enabled": False}]})
    assert list_work.pending and window.plugin_cards.model._timer.active
    list_work.run_tasks()
    window.destroy()


def _exercise_close():
    t = _window()
    window, section = t.window, t.window.plugin_cards
    t.work.run_tasks()
    assert t.bus.handlers
    section.model.schedule_reload()
    assert section.model._timer.active
    section.model.reload()  # still running when the window closes
    window.close()
    _settle()
    assert not t.bus.handlers and not section.model._timer.active
    t.work.run_tasks()
    assert not t.bus.handlers, "a late reload subscribes nothing"
    section.schedule_reload()  # a late settings change after closing
    assert not section.model._timer.active


def _exercise_remembered():
    # Unfolded last time: unfolded again, without writing anything.
    t = _window(saved={"plugins_tools_expanded": True})
    assert t.window.extras.get_reveal_child() and t.window.extras_button.get_active()
    assert t.ui_state.saved == []
    t.window.destroy()


if __name__ == "__main__":
    {
        "cards": _exercise_cards,
        "send": _exercise_send,
        "tools": _exercise_tools,
        "photos": _exercise_photos,
        "close": _exercise_close,
        "remembered": _exercise_remembered,
    }[sys.argv[1]]()
