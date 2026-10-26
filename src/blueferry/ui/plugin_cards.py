"""The "From Plugins" section of the GTK client (PLUGINS.md, ApiVersion 1.2).

GTK counterpart of the Qt client's ``PluginCardSection.qml`` and
``qt/plugin_surfaces.py``, on the shared
:class:`blueferry.plugin_surfaces_model.PluginSurfacesModel`: the items every
enabled card plugin offers, each with its action buttons (a click on the
row runs the primary one), a dimmed line for a plugin that failed, and
"Send to…" with the targets of every share plugin. An action with
``sendTo`` (ApiVersion 1.4) asks for files with a ``GtkFileDialog`` and
sends them to that target; local files dropped on such an item go to the
same target.

``Plugin1.CardChanged`` is heard only from the enabled card plugins
(:class:`blueferry.ui.card_watch.CardChangeWatcher`).
Every text is plain; icons passed :func:`blueferry.plugin_api.surfaces.icon_name`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from blueferry.gio_dbus import GioBus
from blueferry.i18n import _
from blueferry.plugin_api.surfaces import icon_name
from blueferry.plugin_surfaces_model import PluginSurfacesModel
from blueferry.ui.card_watch import CardChangeWatcher
from blueferry.ui.util import glib_timer, launch_uri

SurfacesFactory = Callable[
    [Callable[[], None], Callable[[str], None], Callable[[list[str]], None]],
    PluginSurfacesModel,
]
# choose_files(parent, title, done(paths)): paths of local files only.
FileChooser = Callable[[Gtk.Widget, str, Callable[[list[str]], None]], None]


def default_surfaces(client) -> SurfacesFactory:
    def make(on_change, opener, watch) -> PluginSurfacesModel:
        return PluginSurfacesModel(
            runner=client.task_runner("surfaces", 2), timer=glib_timer, opener=opener,
            on_change=on_change, watch=watch,
        )

    return make


def local_paths(files: Sequence[Gio.File]) -> list[str] | None:
    """Paths of local files, or ``None`` when anything is not a local file
    (a link dragged from a browser is no file)."""
    paths = [file.get_path() for file in files]
    if not paths or any(path is None for path in paths):
        return None
    return [str(path) for path in paths]


def choose_files(parent: Gtk.Widget, title: str, done: Callable[[list[str]], None]) -> None:
    """Ask for files with the portal-aware ``GtkFileDialog``."""
    root = parent.get_root()
    window = root if isinstance(root, Gtk.Window) else None
    dialog = Gtk.FileDialog(title=title, modal=True)

    def finished(source: Gtk.FileDialog, result) -> None:
        try:
            files = source.open_multiple_finish(result)
        except GLib.Error:
            return  # cancelled
        chosen = [files.get_item(index) for index in range(files.get_n_items())]
        paths = local_paths([file for file in chosen if file is not None])
        if paths:
            done(paths)

    dialog.open_multiple(window, None, finished)


def _icon(name: object, fallback: str) -> Gtk.Image:
    return Gtk.Image(icon_name=icon_name(name) or fallback)


class PluginCardSection(Gtk.Box):
    def __init__(
        self,
        surfaces: SurfacesFactory,
        *,
        bus: GioBus | None = None,
        files: FileChooser = choose_files,
        opener: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._files = files
        self._launch = opener or (lambda uri: launch_uri(self, uri))
        self.watcher = CardChangeWatcher(self._card_changed, bus)
        self._cards_key = ""
        self._targets_key = ""
        self._item_rows: list[Gtk.Widget] = []
        self._buttons: list[Gtk.Widget] = []
        self.drop_targets: dict[str, Gtk.DropTarget] = {}
        self._stopped = False

        header = Gtk.Box(spacing=6)
        header.append(Gtk.Label(
            label=_("From Plugins"), xalign=0, hexpand=True, css_classes=["heading"],
        ))
        self.refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"), css_classes=["flat"],
        )
        self.refresh_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Refresh")])
        self.refresh_button.connect("clicked", lambda _b: self.model.reload())
        header.append(self.refresh_button)
        self.append(header)

        self.list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
        )
        self.append(self.list)

        # "Send to…": the targets of every share plugin, asked for (which
        # starts those plugins) only when the menu opens.
        self.targets = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.send_popover = Gtk.Popover(child=self.targets)
        self.send_popover.connect("show", lambda _p: self.model.load_targets())
        self.send_button = Gtk.MenuButton(
            icon_name="document-send-symbolic", popover=self.send_popover,
            valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text=_("Send to…"),
        )
        self.send_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Send to…")])
        self.send_row = Adw.ActionRow(
            title=_("Send to…"), use_markup=False, subtitle_lines=2,
            activatable_widget=self.send_button,
        )
        self.send_row.add_prefix(Gtk.Image(icon_name="document-send-symbolic"))
        self.send_row.add_suffix(self.send_button)

        self.message_row = Adw.ActionRow(use_markup=False, title_lines=0)
        self.message_icon = Gtk.Image()
        self.message_row.add_prefix(self.message_icon)
        close = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text=_("Dismiss"),
            valign=Gtk.Align.CENTER, css_classes=["flat"],
        )
        close.update_property([Gtk.AccessibleProperty.LABEL], [_("Dismiss")])
        close.connect("clicked", lambda _b: self.model.clear_message())
        self.message_row.add_suffix(close)

        self.model = surfaces(self._render, self._open, self.watcher.watch)
        self._render()
        self.model.reload()

    # ---- the window's side ------------------------------------------------

    def schedule_reload(self) -> None:
        """A plugin was enabled, disabled, installed or removed."""
        if not self._stopped:
            self.model.schedule_reload()

    def stop(self) -> None:
        """The window closes: no more signals, no pending reload."""
        self._stopped = True
        self.watcher.stop()
        self.model.stop()

    def _card_changed(self) -> None:
        if not self._stopped:
            self.model.schedule_reload()

    def _open(self, uri: str) -> None:
        # Only what the model hands over after the plugin re-check.
        if not self._stopped:
            self._launch(uri)

    # ---- actions ----------------------------------------------------------------

    def run_action(self, plugin_id: str, item: dict, action: dict) -> None:
        """One click: send files (``sendTo``) or let the plugin act."""
        target = str(action.get("sendTo") or "")
        if target:
            label = str(item.get("title") or "")
            self._files(
                self, _("Send Files to {target}").format(target=label),
                lambda paths: self.model.send_from_card(plugin_id, target, label, paths),
            )
            return
        self.model.invoke(plugin_id, str(item["id"]), str(action["id"]))

    def choose_target(self, key: str) -> None:
        self.send_popover.popdown()
        self._files(self, _("Choose Files to Send"), lambda paths: self.model.send(key, paths))

    def _dropped(self, plugin_id: str, item: dict, value) -> bool:
        if self.model.state()["busy"] or not isinstance(value, Gdk.FileList):
            return False
        paths = local_paths(value.get_files())
        if paths is None:
            return False
        self.model.send_from_card(plugin_id, str(item["dropTarget"]), str(item["title"]), paths)
        return True

    # ---- rendering ----------------------------------------------------------------

    def _render(self) -> None:
        state = self.model.state()
        busy = bool(state["busy"])
        cards = state["cards"]
        targets = state["targets"]
        can_share = state["shareAvailable"] is True or bool(targets)
        key = json.dumps(cards, sort_keys=True)
        if key != self._cards_key:
            self._cards_key = key
            self._build(cards)
        self.send_row.set_visible(can_share)
        self.send_row.set_subtitle(
            ", ".join(target["label"] for target in targets) if targets
            else _("Send files to a plugin target"))
        self._render_targets(state)
        message = state["message"]
        self.message_row.set_visible(bool(message))
        self.message_row.set_title(message)
        ok = state["messageOk"] is not False
        self.message_icon.set_from_icon_name(
            "object-select-symbolic" if ok else "dialog-warning-symbolic")
        for name in ("success", "warning"):
            self.message_icon.remove_css_class(name)
        self.message_icon.add_css_class("success" if ok else "warning")
        self.refresh_button.set_sensitive(not busy)
        self.send_row.set_sensitive(not busy)
        for widget in self._item_rows:
            widget.set_sensitive(not busy)
        self.set_visible(bool(cards) or can_share or bool(message))

    def _build(self, cards: list[dict]) -> None:
        child = self.list.get_first_child()
        while child is not None:
            self.list.remove(child)
            child = self.list.get_first_child()
        self._item_rows, self.drop_targets = [], {}
        for card in cards:
            if len(cards) > 1 and card.get("ok"):
                # The plugin's name when there are several, like a sub-heading.
                heading = Gtk.ListBoxRow(activatable=False, selectable=False, child=Gtk.Label(
                    label=card["name"], xalign=0, use_markup=False,
                    css_classes=["dim-label", "caption"],
                    margin_top=6, margin_bottom=2, margin_start=12, margin_end=12,
                ))
                self.list.append(heading)
            if not card.get("ok"):
                hint = Adw.ActionRow(
                    title=card["name"], subtitle=card.get("hint") or "", use_markup=False,
                    subtitle_lines=0, activatable=False, css_classes=["dim-label"],
                )
                hint.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                self.list.append(hint)
            for item in card.get("items") or []:
                self.list.append(self._item_row(card["pluginId"], item))
        self.list.append(self.send_row)
        self.list.append(self.message_row)

    def _item_row(self, plugin_id: str, item: dict) -> Adw.ActionRow:
        actions = item.get("actions") or []
        primary = next((action for action in actions if action.get("primary")), None)
        row = Adw.ActionRow(
            title=item["title"], subtitle=item.get("subtitle") or "", use_markup=False,
            subtitle_lines=2, activatable=primary is not None,
        )
        if primary is not None:
            # A click on the row runs the item's primary action.
            row.connect("activated", lambda _r: self.run_action(plugin_id, item, primary))
        row.add_prefix(_icon(item.get("icon"), "application-x-addon-symbolic"))
        for action in actions:
            if action is primary:
                continue
            row.add_suffix(self._action_button(plugin_id, item, action, ""))
        if primary is not None:
            row.add_suffix(self._action_button(plugin_id, item, primary, "go-next-symbolic"))
        if item.get("dropTarget"):
            # Files dragged from the file manager onto a device.
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", lambda _t, value, _x, _y: self._dropped(plugin_id, item, value))
            row.add_controller(drop)
            self.drop_targets[str(item["id"])] = drop
        self._item_rows.append(row)
        return row

    def _action_button(self, plugin_id: str, item: dict, action: dict, fallback: str) -> Gtk.Button:
        label = str(action.get("labelPlain") or "")
        icon = icon_name(action.get("icon")) or fallback
        button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text=label)
        if icon:
            button.set_icon_name(icon)
        else:
            button.set_label(label)
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        button.connect("clicked", lambda _b: self.run_action(plugin_id, item, action))
        return button

    def _render_targets(self, state: dict) -> None:
        targets = state["targets"]
        loading = state["targetsLoading"] is True
        key = json.dumps([targets, loading, state["targetsLoaded"]], sort_keys=True)
        if key == self._targets_key:
            return
        self._targets_key = key
        child = self.targets.get_first_child()
        while child is not None:
            self.targets.remove(child)
            child = self.targets.get_first_child()
        note = ""
        if loading and not targets:
            note = _("Looking for targets…")
        elif not loading and state["targetsLoaded"] and not targets:
            note = _("No targets right now")
        if note:
            self.targets.append(Gtk.ListBoxRow(activatable=False, selectable=False, child=Gtk.Label(
                label=note, css_classes=["dim-label"], margin_top=6, margin_bottom=6,
                margin_start=12, margin_end=12,
            )))
        for target in targets:
            row = Adw.ActionRow(title=target["label"], use_markup=False, activatable=True)
            row.add_prefix(_icon(target.get("icon"), "document-send-symbolic"))
            row.connect("activated", lambda _r, key=target["key"]: self.choose_target(key))
            self.targets.append(row)
