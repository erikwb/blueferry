"""The Photos tab of the GTK client (a plugin with the ``photos`` capability).

GTK counterpart of the Qt client's ``PhotosTab.qml``, on the shared
:class:`blueferry.photos_model.PhotosTabModel`. The newest photos are read
only while the tab is shown and forgotten when it is hidden; thumbnails
are local cache files the plugin client already checked and are decoded off
the GTK thread. Without a plugin, or before its setup, the tab says why. A
click downloads the original (a spinner in the header shows the download)
and opens it in the default viewer; a downloaded original can be dragged
into other apps. Labels are plain text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from blueferry.client_tasks import TaskRunner
from blueferry.i18n import _
from blueferry.photos_model import PhotosTabModel

THUMBNAIL_SIZE = 256
CELL_SIZE = 132
ThumbnailLoader = Callable[[str, Gio.Cancellable, Callable[[Gdk.Texture | None], None]], None]
FILTERS = ("all", "photos", "videos")


def file_url(path: object) -> str:
    """A local path as a file URI ("" for none)."""
    return Gio.File.new_for_path(str(path)).get_uri() if path else ""


def shown_items(items: list[dict], kind: str) -> list[dict]:
    """The items of one filter (All, Photos, Videos)."""
    if kind == "all":
        return list(items)
    return [item for item in items if (kind == "videos") == (item.get("video") is True)]


def placeholder(state: dict, kind: str) -> tuple[str, str]:
    """Title and explanation of the empty tab."""
    if state.get("ready") is True:
        return (_("No videos") if kind == "videos" else _("No photos")), ""
    title = (_("Photos are not available") if state.get("present") is True
             else _("No photo plugin"))
    return title, str(state.get("hint") or "")


def load_thumbnail(uri: str, cancellable: Gio.Cancellable,
                   done: Callable[[Gdk.Texture | None], None]) -> None:
    """Decode a local thumbnail asynchronously (GIO and GdkPixbuf threads)."""
    if not uri.startswith("file:"):
        done(None)
        return

    def decoded(_source, result) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream_finish(result)
        except GLib.Error:
            done(None)
            return
        memory = (Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha()
                  else Gdk.MemoryFormat.R8G8B8)
        done(Gdk.MemoryTexture.new(
            pixbuf.get_width(), pixbuf.get_height(), memory, pixbuf.read_pixel_bytes(),
            pixbuf.get_rowstride(),
        ))

    def opened(file: Gio.File, result) -> None:
        try:
            stream = file.read_finish(result)
        except GLib.Error:
            done(None)
            return
        GdkPixbuf.Pixbuf.new_from_stream_at_scale_async(
            stream, THUMBNAIL_SIZE, THUMBNAIL_SIZE, True, cancellable, decoded)

    Gio.File.new_for_uri(uri).read_async(GLib.PRIORITY_LOW, cancellable, opened)


def launch_file(parent: Gtk.Widget, path: object) -> None:
    root = parent.get_root()
    window = root if isinstance(root, Gtk.Window) else None

    def finished(launcher: Gtk.FileLauncher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error:
            pass  # the viewer reports its own problems

    Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(window, None, finished)


class CountingRunner:
    """A TaskRunner that tells how many of its tasks are still running."""

    def __init__(self, runner: TaskRunner, changed: Callable[[int], None]) -> None:
        self._runner = runner
        self._changed = changed
        self.running = 0

    def submit(self, operation, on_done=None, on_error=None, on_finished=None) -> None:
        self.running += 1
        self._changed(self.running)

        def finished() -> None:
            self.running -= 1
            self._changed(self.running)
            if on_finished is not None:
                on_finished()

        self._runner.submit(operation, on_done, on_error, finished)


class PhotosPage(Gtk.Box):
    def __init__(
        self,
        runner: TaskRunner,
        open_runner: TaskRunner,
        *,
        open_file: Callable[[object], None] | None = None,
        thumbnails: ThumbnailLoader = load_thumbnail,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._thumbnails = thumbnails
        self._open_file = open_file or (lambda path: launch_file(self, path))
        self._filter = "all"
        self._cancellable = Gio.Cancellable()
        self._items_key: object = None
        self.cells: dict[str, Gtk.FlowBoxChild] = {}
        self._order: list[str] = []
        self.pictures: dict[str, Gtk.Picture] = {}
        self.originals = CountingRunner(open_runner, self._downloads_changed)
        self.model = PhotosTabModel(
            runner=runner, open_runner=self.originals, url=file_url,
            open_file=self._opened, on_change=self._render,
        )

        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            margin_top=12, margin_bottom=6, margin_start=18, margin_end=12,
        )
        header.append(Gtk.Label(
            label=_("Recent Photos"), xalign=0, hexpand=True, css_classes=["heading"],
        ))
        self.filters = Gtk.Box(css_classes=["linked"])
        self.filter_buttons: dict[str, Gtk.ToggleButton] = {}
        group: Gtk.ToggleButton | None = None
        for key, label in zip(FILTERS, (_("All"), _("Photos"), _("Videos")), strict=True):
            button = Gtk.ToggleButton(label=label, active=key == "all", group=group)
            group = group or button
            button.connect("toggled", lambda b, k=key: b.get_active() and self._filtered(k))
            self.filters.append(button)
            self.filter_buttons[key] = button
        header.append(self.filters)
        self.download_spinner = Gtk.Spinner(
            valign=Gtk.Align.CENTER, visible=False, tooltip_text=_("Downloading the original…"),
        )
        header.append(self.download_spinner)
        self.refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"), css_classes=["flat"],
        )
        self.refresh_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Refresh")])
        self.refresh_button.connect("clicked", lambda _b: self.model.refresh())
        header.append(self.refresh_button)
        self.append(header)

        self.summary = Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=False,
            css_classes=["dim-label", "caption"], margin_start=18, margin_end=12,
        )
        self.append(self.summary)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Gtk.Spinner(
            spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            width_request=32, height_request=32,
        ), "loading")
        empty = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, margin_start=24, margin_end=24,
        )
        self.empty_icon = Gtk.Image(icon_name="folder-pictures-symbolic", pixel_size=64,
                                    css_classes=["dim-label"])
        self.empty_title = Gtk.Label(use_markup=False, wrap=True, css_classes=["title-2"])
        # The hint may carry the plugin's own text: a plain label, never markup.
        self.empty_hint = Gtk.Label(
            use_markup=False, wrap=True, justify=Gtk.Justification.CENTER,
            max_width_chars=60, selectable=True, css_classes=["dim-label"],
        )
        for widget in (self.empty_icon, self.empty_title, self.empty_hint):
            empty.append(widget)
        self.stack.add_named(empty, "empty")
        self.grid = Gtk.FlowBox(
            homogeneous=True, selection_mode=Gtk.SelectionMode.NONE,
            activate_on_single_click=True, valign=Gtk.Align.START,
            min_children_per_line=2, max_children_per_line=16,
            row_spacing=6, column_spacing=6, margin_start=12, margin_end=12, margin_bottom=12,
        )
        self.grid.connect("child-activated", self._activated)
        self.stack.add_named(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid,
        ), "grid")
        self.append(self.stack)
        self._render()

    # ---- the window's side ----------------------------------------------------

    def set_watched(self, watched: bool) -> None:
        """Read while the tab is shown; forget the photos when hidden."""
        if not watched:
            self._cancel_thumbnails()
        self.model.watch(watched)

    def open(self, photo_id: str) -> None:
        self.model.open(photo_id)

    # ---- callbacks ----------------------------------------------------------------

    def _filtered(self, kind: str) -> None:
        self._filter = kind
        self._render()

    def _activated(self, _grid: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        index = child.get_index()
        if 0 <= index < len(self._order):
            self.model.open(self._order[index])

    def _opened(self, path: object) -> None:
        self._open_file(path)

    def _downloads_changed(self, running: int) -> None:
        self.download_spinner.set_visible(running > 0)
        self.download_spinner.set_spinning(running > 0)

    def _cancel_thumbnails(self) -> None:
        self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()

    # ---- rendering ----------------------------------------------------------------

    def _render(self) -> None:
        state: dict[str, Any] = self.model.state
        ready = state.get("ready") is True
        items = shown_items(state.get("items") or [], self._filter)
        self.filters.set_visible(ready)
        self.refresh_button.set_sensitive(state.get("present") is True)
        self.summary.set_visible(ready)
        self.summary.set_label(str(state.get("hint") or "") if ready else "")
        if state.get("loaded") is not True:
            self.stack.set_visible_child_name("loading")
        elif not ready or not items:
            title, hint = placeholder(state, self._filter)
            self.empty_title.set_label(title)
            self.empty_hint.set_label(hint)
            self.empty_hint.set_visible(bool(hint))
            if ready:
                self.empty_icon.remove_css_class("dim-label")
            else:
                self.empty_icon.add_css_class("dim-label")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("grid")
        key = [(item["id"], item["thumbnail"]) for item in items]
        if key != self._items_key:
            self._items_key = key
            self._build(items)
        else:
            for item in items:
                self._update_cell(item)

    def _build(self, items: list[dict]) -> None:
        self._cancel_thumbnails()
        self.grid.remove_all()
        self.cells, self.pictures = {}, {}
        self._order = [str(item["id"]) for item in items]
        cancellable = self._cancellable
        for item in items:
            photo_id = str(item["id"])
            picture = Gtk.Picture(
                content_fit=Gtk.ContentFit.COVER, can_shrink=True,
                width_request=CELL_SIZE, height_request=CELL_SIZE,
            )
            # The clamps keep a large thumbnail from widening its cell.
            square = Adw.Clamp(
                orientation=Gtk.Orientation.VERTICAL, maximum_size=CELL_SIZE,
                tightening_threshold=CELL_SIZE, child=picture,
            )
            overlay = Gtk.Overlay(child=Adw.Clamp(
                maximum_size=CELL_SIZE, tightening_threshold=CELL_SIZE, child=square,
            ), css_classes=["card"], overflow=Gtk.Overflow.HIDDEN, halign=Gtk.Align.CENTER)
            badge = Gtk.Image(
                icon_name="media-playback-start-symbolic" if item.get("video")
                else "image-x-generic-symbolic",
                pixel_size=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            )
            overlay.add_overlay(badge)
            caption = Gtk.Label(
                label=str(item.get("label") or ""), use_markup=False,
                ellipsize=Pango.EllipsizeMode.END, max_width_chars=18,
                css_classes=["caption"],
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(overlay)
            box.append(caption)
            child = Gtk.FlowBoxChild(child=box)
            drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
            drag.connect("prepare", lambda _s, _x, _y, ident=photo_id: self._drag(ident))
            child.add_controller(drag)
            self.grid.append(child)
            self.cells[photo_id] = child
            self.pictures[photo_id] = picture
            self._update_cell(item)
            video = item.get("video") is True

            def loaded(texture, ident=photo_id, mark=badge, video=video) -> None:
                if cancellable.is_cancelled() or ident not in self.pictures:
                    return
                if texture is not None:
                    self.pictures[ident].set_paintable(texture)
                    mark.set_visible(video)

            if item.get("thumbnail"):
                self._thumbnails(str(item["thumbnail"]), cancellable, loaded)

    def _update_cell(self, item: dict) -> None:
        child = self.cells.get(str(item["id"]))
        if child is None:
            return
        child.set_tooltip_text(
            _("Click to open, or drag the file into another app") if item.get("original")
            else _("Click to download and open"))

    def _drag(self, photo_id: str) -> Gdk.ContentProvider | None:
        """Drag the original into other apps once it is downloaded."""
        item = next((entry for entry in self.model.state.get("items") or []
                     if str(entry["id"]) == photo_id), None)
        original = str((item or {}).get("original") or "")
        if not original.startswith("file:"):
            return None
        files = Gdk.FileList.new_from_list([Gio.File.new_for_uri(original)])
        return Gdk.ContentProvider.new_for_value(files)
