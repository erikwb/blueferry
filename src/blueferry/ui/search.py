"""The global search dialog (``Ctrl+K`` or ``Ctrl+F``).

GTK counterpart of the Qt client's ``SearchDialog.qml``, on the shared
:class:`blueferry.search_model.GlobalSearch`; sections, rows and wording
come from :mod:`blueferry.search_view`. The search runs after a short pause
in typing; choosing a hit closes the dialog and hands its target
(conversation, message, contact or call) to the window. Closing the dialog
forgets the query and the results. Every field comes from the iPhone and is
escaped; only the match in a message snippet is set in bold.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gdk, GLib, Gtk

from blueferry import search_view
from blueferry.client_tasks import TimerFactory
from blueferry.i18n import _
from blueferry.search_model import GlobalSearch
from blueferry.ui.util import glib_timer

_ICONS = {
    "conversation": "chat-message-new-symbolic",
    "message": "mail-unread-symbolic",
    "contact": "avatar-default-symbolic",
    "call": "call-start-symbolic",
}


def snippet_markup(snippet: str, highlight: tuple[int, int] | None) -> str:
    """Pango markup of a snippet with the match in bold; everything escaped."""
    if not highlight:
        return GLib.markup_escape_text(snippet)
    start, length = highlight
    end = start + length
    return (
        GLib.markup_escape_text(snippet[:start])
        + "<b>" + GLib.markup_escape_text(snippet[start:end]) + "</b>"
        + GLib.markup_escape_text(snippet[end:])
    )


class SearchDialog(Adw.Dialog):
    def __init__(
        self,
        client,
        *,
        activate: Callable[[dict[str, Any]], None],
        timer: TimerFactory | None = None,
    ) -> None:
        super().__init__(
            title=_("Search"), content_width=560, content_height=600,
        )
        self._activate = activate
        self.model = GlobalSearch(
            runner=client, timer=timer or glib_timer, fetch=client.search,
            on_change=self._render,
        )
        self._rows: list[Adw.ActionRow] = []

        self.entry = Gtk.SearchEntry(
            placeholder_text=search_view.placeholder(), hexpand=True,
            tooltip_text=search_view.scope_text(),
        )
        self.entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [search_view.placeholder()],
        )
        # The model waits for a pause in typing before it searches.
        self.entry.connect("changed", lambda entry: self.model.set_query(entry.get_text()))
        self.entry.connect("activate", lambda _entry: self.activate_row(0))
        self.entry.connect("stop-search", lambda _entry: self.close())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._entry_key)
        self.entry.add_controller(keys)

        header = Adw.HeaderBar(title_widget=self.entry)
        self.error_banner = Adw.Banner(use_markup=False)
        self.limit_label = Gtk.Label(
            xalign=0, wrap=True, margin_start=18, margin_end=18, margin_top=6,
            css_classes=["dim-label", "caption"], visible=False,
        )
        self.groups = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=12, margin_bottom=18, margin_start=12, margin_end=12,
        )
        self.placeholder = Adw.StatusPage(
            icon_name="system-search-symbolic", css_classes=["compact"], vexpand=True,
        )
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=Adw.Clamp(maximum_size=640, child=self.groups),
        ), "results")
        self.stack.add_named(self.placeholder, "placeholder")
        self.stack.add_named(Gtk.Spinner(
            spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            width_request=32, height_request=32,
        ), "loading")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.error_banner)
        body.append(self.limit_label)
        body.append(self.stack)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.set_child(toolbar)
        self.connect("closed", lambda _dialog: self._forget())
        self._render()

    # ---- the window's side ---------------------------------------------

    def apply_status(self, status: dict) -> None:
        self.model.apply_status(status)

    def invalidate(self) -> None:
        """History or call history changed: drop the shown hits, search again."""
        self.model.invalidate()

    def open(self, parent: Gtk.Widget) -> None:
        self.entry.set_text("")
        self.present(parent)
        self.entry.grab_focus()

    def dismiss(self) -> None:
        """The window is no longer shown: close and forget the search."""
        if self.get_root() is not None:
            self.close()
        self._forget()

    def activate_row(self, index: int) -> None:
        """Open hit ``index`` (in display order) and close the search."""
        target = self.model.target_at(index)
        if target is None:
            return
        self.close()
        self._activate(target)

    # ---- internals ------------------------------------------------------

    def _forget(self) -> None:
        self.entry.set_text("")
        self.model.clear()

    def _entry_key(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Down and self._rows:
            self._rows[0].grab_focus()
            return True
        return False

    def _render(self) -> None:
        view = self.model.view()
        self.error_banner.set_title(view["error"])
        self.error_banner.set_revealed(bool(view["error"]))
        self.limit_label.set_label(view["limit_text"])
        self.limit_label.set_visible(bool(view["limit_text"]))
        child = self.groups.get_first_child()
        while child is not None:
            self.groups.remove(child)
            child = self.groups.get_first_child()
        self._rows = []
        for section in view["sections"]:
            title = section["title"]
            if section["more"]:
                title = f"{title} · {section['more']}"
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
            for item in section["rows"]:
                row = self._hit_row(item, len(self._rows))
                self._rows.append(row)
                group.add(row)
            self.groups.append(group)
        if self._rows:
            self.stack.set_visible_child_name("results")
        elif view["loading"]:
            self.stack.set_visible_child_name("loading")
        else:
            if not view["supported"]:
                title, text = _("Search Unavailable"), _(
                    "The running BlueFerry service cannot search. "
                    "Restart it after upgrading."
                )
            else:
                title, text = _("Search"), view["hint"] or view["scope"]
            self.placeholder.set_title(title)
            self.placeholder.set_description(GLib.markup_escape_text(text) or None)
            self.stack.set_visible_child_name("placeholder")

    def _hit_row(self, item: dict[str, Any], index: int) -> Adw.ActionRow:
        lines = [GLib.markup_escape_text(item["subtitle"])] if item["subtitle"] else []
        if item["snippet"]:
            lines.append(snippet_markup(item["snippet"], item["highlight"]))
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(item["title"]), subtitle="\n".join(lines),
            use_markup=True, activatable=True, subtitle_lines=3,
        )
        row.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(
            part for part in (item["title"], item["subtitle"], item["snippet"], item["time"])
            if part
        )])
        row.add_prefix(Gtk.Image(icon_name=_ICONS.get(item["target"]["kind"], "")))
        if item["time"]:
            row.add_suffix(Gtk.Label(
                label=item["time"], css_classes=["dim-label", "numeric"],
                valign=Gtk.Align.START,
            ))
        row.connect("activated", lambda _row: self.activate_row(index))
        return row
