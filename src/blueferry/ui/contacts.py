"""The Contacts tab: the cached iPhone contact cards and one open card.

GTK counterpart of the Qt client's ``ContactsTab.qml``, on the shared
:class:`blueferry.contacts_model.ContactBook`; rows, sections and wording
come from :mod:`blueferry.contacts_view`. The cards are read only while the
tab is shown (:meth:`ContactsPage.set_watched`) and forgotten, with the open
card and its photo, when it is hidden. The search field reloads the list
after a short pause; further pages load when the list is scrolled to its
end. **Call** goes through the window's confirmation dialog, **Message**
opens the conversation with that number or a new message. Every field
comes from the iPhone and is shown as plain text; the photo is decoded by
the bounded avatar decoder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from blueferry import contacts_view, phone_link_view
from blueferry.client_tasks import TimerFactory
from blueferry.contacts_model import ContactBook
from blueferry.i18n import _
from blueferry.ui.avatars import decode_avatar
from blueferry.ui.util import glib_timer

_END = Pango.EllipsizeMode.END
_ACTION_ICONS = {
    contacts_view.ACTION_DIAL: "call-start-symbolic",
    contacts_view.ACTION_MESSAGE: "chat-message-new-symbolic",
    contacts_view.ACTION_MAIL: "mail-message-new-symbolic",
    contacts_view.ACTION_COPY: "edit-copy-symbolic",
}


def _action_label(action: str) -> str:
    return {
        contacts_view.ACTION_DIAL: _("Call"),
        contacts_view.ACTION_MESSAGE: _("Send a message"),
        contacts_view.ACTION_MAIL: _("Write an email"),
    }.get(action, _("Copy"))


def primary_number(card: dict[str, Any]) -> str:
    """The first number of the card that can be dialled or messaged."""
    for section in card.get("sections") or []:
        if section.get("id") != contacts_view.SECTION_PHONES:
            continue
        for row in section.get("rows") or []:
            if contacts_view.ACTION_DIAL in row.get("actions", []):
                return str(row.get("target") or "")
    return ""


def avatar_text(initials: str) -> str:
    """Text for an ``Adw.Avatar`` that shows exactly the presenter's initials.

    Adw.Avatar derives its own initials from the first and last word of its
    text; spacing the letters makes it show ``contacts_view.initials`` (the
    ones Qt, the terminal and Quickshell show) instead of guessing from the
    title, which may be a number or contain markup characters.
    """
    return " ".join(initials)


class ContactsPage(Gtk.Box):
    def __init__(
        self,
        client,
        toast: Callable[[str], None],
        *,
        dial: Callable[[str], None],
        message: Callable[[str], None],
        timer: TimerFactory | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._toast = toast
        self._dial = dial
        self._message = message
        self._shown_rows: list[dict[str, Any]] = []
        self._shown_card: dict[str, Any] | None = None
        self.book = ContactBook(
            runner=client, timer=timer or glib_timer,
            fetch_page=client.contact_cards, fetch_card=client.contact,
            on_change=self._render,
        )

        # ---- left: search and list -------------------------------------
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=contacts_view.search_placeholder(),
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
        )
        self.search_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [contacts_view.search_placeholder()],
        )
        # The model waits for a pause in typing before it reads.
        self.search_entry.connect(
            "changed", lambda entry: self.book.set_query(entry.get_text()),
        )
        sidebar.append(self.search_entry)
        self.count_label = Gtk.Label(
            xalign=0, margin_start=12, margin_end=12, margin_bottom=6,
            css_classes=["dim-label", "caption"], ellipsize=_END,
        )
        sidebar.append(self.count_label)
        self.error_banner = Adw.Banner(use_markup=False, button_label=_("Try Again"))
        self.error_banner.connect("button-clicked", lambda _b: self.book.reload())
        sidebar.append(self.error_banner)

        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.list.connect("row-activated", self._row_activated)
        self.more_button = Gtk.Button(
            label=_("Show More Contacts"), css_classes=["flat"],
            margin_top=6, margin_bottom=6, visible=False,
        )
        self.more_button.connect("clicked", lambda _b: self.book.load_more())
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        listing.append(self.list)
        listing.append(self.more_button)
        self.list_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=240,
            hexpand=True, vexpand=True, child=listing,
        )
        self.list_scroll.connect("edge-reached", self._edge_reached)
        self.list_placeholder = Adw.StatusPage(
            icon_name="avatar-default-symbolic", css_classes=["compact"], vexpand=True,
        )
        self.list_stack = Gtk.Stack(vexpand=True)
        self.list_stack.add_named(self.list_scroll, "list")
        self.list_stack.add_named(self.list_placeholder, "placeholder")
        self.list_stack.add_named(Gtk.Spinner(
            spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            width_request=32, height_request=32,
        ), "loading")
        sidebar.append(self.list_stack)
        sidebar_page = Adw.NavigationPage(child=sidebar, title=_("Contacts"))

        # ---- right: the open card --------------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        self.back_button = Gtk.Button(
            icon_name="go-previous-symbolic", tooltip_text=_("Back to Contacts"),
            valign=Gtk.Align.CENTER, visible=False,
        )
        self.back_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Back to Contacts")],
        )
        self.back_button.connect("clicked", lambda _b: self._back())
        header.append(self.back_button)
        self.avatar = Adw.Avatar(size=64, show_initials=True)
        header.append(self.avatar)
        titles = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER,
        )
        self.title_label = Gtk.Label(
            xalign=0, css_classes=["title-2"], ellipsize=_END, selectable=True,
        )
        self.subtitle_label = Gtk.Label(
            xalign=0, css_classes=["dim-label"], ellipsize=_END, selectable=True,
        )
        titles.append(self.title_label)
        titles.append(self.subtitle_label)
        header.append(titles)
        self.call_button = Gtk.Button(
            label=_("Call"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"],
        )
        self.call_button.connect("clicked", lambda _b: self._run(
            contacts_view.ACTION_DIAL, primary_number(self.book.card)))
        header.append(self.call_button)
        self.message_button = Gtk.Button(label=_("Message"), valign=Gtk.Align.CENTER)
        self.message_button.connect("clicked", lambda _b: self._run(
            contacts_view.ACTION_MESSAGE, primary_number(self.book.card)))
        header.append(self.message_button)
        right.append(header)
        self.card_banner = Adw.Banner(use_markup=False, button_label=_("Reload"))
        self.card_banner.connect("button-clicked", lambda _b: self._reload_after_stale())
        right.append(self.card_banner)
        self.sections = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=6, margin_bottom=18, margin_start=12, margin_end=12,
        )
        self.card_stack = Gtk.Stack(vexpand=True)
        self.card_stack.add_named(Adw.StatusPage(
            icon_name="avatar-default-symbolic", title=_("Select a Contact"),
            css_classes=["compact"], vexpand=True,
        ), "empty")
        self.card_stack.add_named(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=Adw.Clamp(maximum_size=720, child=self.sections),
        ), "card")
        self.card_header = header
        right.append(self.card_stack)
        content_page = Adw.NavigationPage(child=right, title=_("Contact"))

        self.split_view = Adw.NavigationSplitView(
            sidebar=sidebar_page, content=content_page,
            min_sidebar_width=220, max_sidebar_width=340,
            hexpand=True, vexpand=True,
        )
        self.unsupported = Adw.StatusPage(
            icon_name="avatar-default-symbolic", title=_("Contacts Unavailable"),
            description=GLib.markup_escape_text(_(
                "The running BlueFerry service does not offer contact cards. "
                "Restart it after upgrading."
            )),
            vexpand=True,
        )
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.split_view, "contacts")
        self.stack.add_named(self.unsupported, "unsupported")
        self.append(self.stack)
        self._render()

    # ---- the window's side ---------------------------------------------

    def apply_status(self, status: dict) -> None:
        """A status read by the phone card: capability and card epoch."""
        self.book.apply_status(status)
        self._render()

    def set_watched(self, watched: bool) -> None:
        self.book.watch(watched)
        self._render()

    def open_contact(self, contact_id: str) -> None:
        """Show one card (from the global search)."""
        self.book.open(contact_id)
        self.split_view.set_show_content(True)

    # ---- actions --------------------------------------------------------

    def _row_activated(self, _list, row) -> None:
        contact_id = getattr(row, "contact_id", "")
        if contact_id:
            self.open_contact(contact_id)

    def _edge_reached(self, _scroll, position) -> None:
        if position == Gtk.PositionType.BOTTOM:
            self.book.load_more()

    def _back(self) -> None:
        self.split_view.set_show_content(False)
        self.book.close()

    def _reload_after_stale(self) -> None:
        self.book.close()
        self.book.reload()

    def _run(self, action: str, target: str) -> None:
        if not target:
            return
        if action == contacts_view.ACTION_DIAL:
            self._dial(phone_link_view.dialable(target))
        elif action == contacts_view.ACTION_MESSAGE:
            self._message(target)
        elif action == contacts_view.ACTION_MAIL:
            Gtk.UriLauncher.new("mailto:" + quote(target, safe="@")).launch(
                self.get_root(), None, None, None,
            )
        elif action == contacts_view.ACTION_COPY:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(target))
            self._toast(_("Copied"))

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        view = self.book.view()
        self.stack.set_visible_child_name("contacts" if view["supported"] else "unsupported")
        self.count_label.set_label(view["count_text"])
        self.count_label.set_visible(bool(view["count_text"]))
        self.error_banner.set_title(view["error"])
        self.error_banner.set_revealed(bool(view["error"]))
        self.more_button.set_visible(view["has_more"])
        self.more_button.set_sensitive(not view["loading"])
        self._render_rows(view["rows"])
        if view["rows"]:
            self.list_stack.set_visible_child_name("list")
        elif view["loading"]:
            self.list_stack.set_visible_child_name("loading")
        else:
            title = _("No Matching Contacts") if view["query"] else _("No Contacts Yet")
            self.list_placeholder.set_title(title if view["hint"] else "")
            self.list_placeholder.set_description(
                GLib.markup_escape_text(view["hint"]) or None
            )
            self.list_stack.set_visible_child_name("placeholder")
        self._render_card()

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        if rows is self._shown_rows:
            return
        shown = [row["id"] for row in self._shown_rows]
        if shown and [row["id"] for row in rows[:len(shown)]] == shown:
            added = rows[len(shown):]  # a further page: keep the scroll position
        else:
            self.list.remove_all()
            added = rows
        for item in added:
            self.list.append(self._contact_row(item))
        self._shown_rows = rows

    def _contact_row(self, item: dict[str, Any]) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=item["title"], subtitle=item["subtitle"], activatable=True,
            use_markup=False, title_lines=1, subtitle_lines=1,
        )
        row.contact_id = item["id"]
        row.add_prefix(Adw.Avatar(
            size=32, text=avatar_text(item["initials"]), show_initials=bool(item["initials"]),
        ))
        return row

    def _render_card(self) -> None:
        card = self.book.card
        if card is self._shown_card:
            return
        self._shown_card = card
        self.card_banner.set_title(card["error"])
        self.card_banner.set_revealed(card["open"] and bool(card["error"]))
        if not card["open"]:
            self.card_stack.set_visible_child_name("empty")
            self.card_header.set_visible(False)
            return
        self.card_header.set_visible(True)
        self.title_label.set_label(card["title"])
        self.subtitle_label.set_label(card["subtitle"])
        self.subtitle_label.set_visible(bool(card["subtitle"]))
        self.avatar.set_text(avatar_text(card["initials"]))
        self.avatar.set_show_initials(bool(card["initials"]))
        photo = self.book.photo
        self.avatar.set_custom_image(decode_avatar(photo) if photo else None)
        child = self.sections.get_first_child()
        while child is not None:
            self.sections.remove(child)
            child = self.sections.get_first_child()
        for section in card["sections"]:
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(section["title"]))
            for item in section["rows"]:
                group.add(self._detail_row(item))
            self.sections.append(group)
        self._render_card_actions(card)
        self.card_stack.set_visible_child_name("card")

    def _render_card_actions(self, card: dict[str, Any]) -> None:
        # Calling asks first and explains when calls are off or not ready.
        number = primary_number(card) if card["open"] else ""
        for button in (self.call_button, self.message_button):
            button.set_visible(bool(number))

    def _detail_row(self, item: dict[str, Any]) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=item["value"], subtitle=item["label"], use_markup=False,
            title_lines=0, title_selectable=True,
        )
        row.add_css_class("property")
        for action in item["actions"]:
            label = _action_label(action)
            button = Gtk.Button(
                icon_name=_ACTION_ICONS.get(action, "edit-copy-symbolic"),
                tooltip_text=label, valign=Gtk.Align.CENTER,
                css_classes=["flat", "circular"],
            )
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            target = item["target"]
            button.connect("clicked", lambda _b, a=action, t=target: self._run(a, t))
            row.add_suffix(button)
        return row
