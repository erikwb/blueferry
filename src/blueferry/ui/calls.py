"""The Calls tab: a dial pad with contact suggestions and the recent calls.

GTK counterpart of the Qt client's ``CallsTab.qml`` and
``RecentCallsPage.qml``. The wording and rules are shared
(``phone_link_view``, ``phone_overview.call_groups``). Calling always goes
through the window's confirmation dialog (``MainWindow.request_dial``),
which also explains numbers BlueFerry does not dial. The recent-call list
is read only while the tab is shown and the opt-in is on, and forgotten
when the tab is left. Names, numbers and errors are plain text.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from blueferry import phone_link_view
from blueferry import phone_overview as overview
from blueferry.client_tasks import TimerFactory
from blueferry.conversation_state import ConversationState
from blueferry.i18n import _
from blueferry.models import CallHistoryEntry
from blueferry.ui.phone_presenter import direction_icon
from blueferry.ui.reads import CoalescedRead
from blueferry.ui.util import glib_timer

MAX_CALLS = 200
SEARCH_DELAY_MS = 200
MAX_QUERY = 64  # the dial field's length, as in the Qt client
_END = Pango.EllipsizeMode.END


def _icon_button(icon: str, label: str, *classes: str) -> Gtk.Button:
    button = Gtk.Button(
        icon_name=icon, tooltip_text=label, valign=Gtk.Align.CENTER,
        css_classes=list(classes),
    )
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return button


class CallsPage(Gtk.Box):
    def __init__(
        self,
        client,
        toast: Callable[[str], None],
        *,
        dial: Callable[[str], None],
        compose: Callable[[str], None],
        open_settings: Callable[[str], None],
        timer: TimerFactory | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self._client = client
        self._toast = toast
        self._dial = dial
        self._compose = compose
        self._status: dict = {}
        self._watched = False
        self._calls: list[CallHistoryEntry] = []
        self._calls_error = ""
        self._calls_received = False
        self._syncing = False
        self._contacts = ConversationState(select_first=False)
        self._typed = True  # entry changes come from the keyboard
        self._search_timer = (timer or glib_timer)(self._search_contacts)
        self._calls_read = CoalescedRead(
            lambda ok, err: client.call_history_async(MAX_CALLS, ok, err),
            self._calls_loaded, self._calls_failed,
            active=lambda: self._watched and overview.call_history_enabled(self._status),
        )

        self.hint_banner = Adw.Banner(use_markup=False, button_label=_("Open Settings"))
        self.hint_banner.connect("button-clicked", lambda _b: open_settings("calls"))
        self.append(self.hint_banner)

        self.columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.append(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True,
            child=self.columns,
        ))
        self.columns.append(self._build_dial_pad())
        self.separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.columns.append(self.separator)
        self.columns.append(self._build_recent_calls(open_settings))

        client.connect("call-history-changed", lambda _c: self._calls_read.refresh())
        self._render()

    # ---- building -------------------------------------------------------

    def _build_dial_pad(self) -> Gtk.Widget:
        column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.START,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18,
            width_request=300, hexpand=False,  # the recent calls take the rest
        )
        field = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry(
            placeholder_text=_("Number or name"), xalign=0.5, hexpand=True,
            max_length=MAX_QUERY, width_chars=8, css_classes=["title-2", "flat"],
        )
        self.entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Phone number or contact name")],
        )
        self.entry.connect("changed", self._entry_changed)
        self.entry.connect("activate", lambda _entry: self.dial())
        field.append(self.entry)
        self.delete_button = _icon_button("edit-clear-symbolic", _("Delete"), "flat", "circular")
        self.delete_button.connect("clicked", lambda _b: self._delete_last())
        # A long press clears the whole number, as in the Qt client.
        clear = Gtk.GestureLongPress()
        clear.connect("pressed", lambda gesture, *_xy: self._long_press(gesture, ""))
        self.delete_button.add_controller(clear)
        field.append(self.delete_button)
        column.append(field)

        self.suggestions = Gtk.ListBox(
            css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
        )
        self.suggestions.connect("row-activated", self._suggestion_activated)
        column.append(self.suggestions)

        self.pad = Gtk.Grid(
            column_spacing=18, row_spacing=12, halign=Gtk.Align.CENTER,
            column_homogeneous=True,
        )
        self.keys: dict[str, Gtk.Button] = {}
        for index, (key, letters) in enumerate(phone_link_view.DIAL_KEYS):
            self.pad.attach(self._dial_key(key, letters), index % 3, index // 3, 1, 1)
        column.append(self.pad)

        self.call_button = _icon_button(
            "call-start-symbolic", _("Call"), "circular", "suggested-action",
        )
        self.call_button.set_halign(Gtk.Align.CENTER)
        self.call_button.set_size_request(64, 64)
        self.call_button.connect("clicked", lambda _b: self.dial())
        column.append(self.call_button)
        return column

    def _dial_key(self, key: str, letters: str) -> Gtk.Button:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        content.append(Gtk.Label(label=key, css_classes=["title-3"]))
        content.append(Gtk.Label(
            label=letters or " ", css_classes=["caption", "dim-label"],
        ))
        button = Gtk.Button(child=content, css_classes=["circular"])
        button.set_size_request(64, 64)
        label = _("Dial {key}").format(key=key)
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        button.connect("clicked", lambda _b: self.append_key(key))
        if key == "0":
            # A long press on 0 enters "+".
            plus = Gtk.GestureLongPress()
            plus.connect("pressed", lambda gesture, *_xy: self._long_press(gesture, "+"))
            button.add_controller(plus)
        self.keys[key] = button
        return button

    def _build_recent_calls(self, open_settings: Callable[[str], None]) -> Gtk.Widget:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            margin_top=12, margin_bottom=6, margin_start=18, margin_end=12,
        )
        header.append(Gtk.Label(
            label=_("Recent Calls"), xalign=0, hexpand=True, css_classes=["heading"],
        ))
        filters = Gtk.Box(css_classes=["linked"])
        self.all_filter = Gtk.ToggleButton(label=_("All"), active=True)
        self.missed_filter = Gtk.ToggleButton(label=_("Missed"), group=self.all_filter)
        self.missed_filter.connect("toggled", lambda _b: self._render_calls())
        filters.append(self.all_filter)
        filters.append(self.missed_filter)
        header.append(filters)
        self.sync_button = _icon_button(
            "view-refresh-symbolic", _("Refresh from iPhone"), "flat",
        )
        self.sync_button.connect("clicked", lambda _b: self.sync())
        header.append(self.sync_button)
        self.recent_header = header
        column.append(header)

        self.recent_stack = Gtk.Stack(vexpand=True)
        self.recent_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=6, margin_bottom=18, margin_start=12, margin_end=12,
        )
        self.recent_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=Adw.Clamp(maximum_size=720, child=self.recent_list),
        )
        self.recent_stack.add_named(self.recent_scroll, "list")
        self.recent_placeholder = Adw.StatusPage(
            icon_name="call-start-symbolic", css_classes=["compact"], vexpand=True,
        )
        self.recent_stack.add_named(self.recent_placeholder, "placeholder")
        self.recent_off = Adw.StatusPage(
            icon_name="call-start-symbolic", title=_("Recent Calls Are Off"),
            css_classes=["compact"], vexpand=True,
        )
        settings = Gtk.Button(
            label=_("Open Settings"), halign=Gtk.Align.CENTER, css_classes=["pill"],
        )
        settings.connect("clicked", lambda _b: open_settings("calls"))
        self.recent_off.set_child(settings)
        self.recent_stack.add_named(self.recent_off, "off")
        column.append(self.recent_stack)
        return column

    # ---- the window's side ---------------------------------------------

    def apply_status(self, status: dict) -> None:
        """A status read by the phone card."""
        enabled = overview.call_history_enabled(status)
        if enabled != overview.call_history_enabled(self._status):
            self._forget_calls()
        self._status = status
        self._render()
        self._calls_read.refresh()

    def set_watched(self, watched: bool) -> None:
        """Read the recent calls while the tab is shown; forget them after."""
        if watched == self._watched:
            return
        self._watched = watched
        if watched:
            self._calls_read.refresh()
            return
        self._search_timer.stop()
        self._forget_calls()
        self._render_calls()

    # ---- dialing --------------------------------------------------------

    @property
    def calls_hint(self) -> str:
        return phone_link_view.calls_hint(self._status, str(self._status.get("calls_state") or ""))

    def dial(self) -> None:
        number = phone_link_view.dialable(self.entry.get_text())
        if number and not self.calls_hint:
            self._dial(number)

    def append_key(self, key: str) -> None:
        self._set_text(phone_link_view.dialable(self.entry.get_text() + key))

    def _set_text(self, text: str) -> None:
        """Change the field from the pad: no contact search, as in Qt."""
        self._typed = False
        try:
            self.entry.set_text(text)
        finally:
            self._typed = True
        self.entry.set_position(-1)

    def _long_press(self, gesture: Gtk.GestureLongPress, key: str) -> None:
        """Long press: 0 enters "+", Delete clears the field."""
        # Claimed, the button does not also report a click on release.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if key:
            self.append_key(key)
        else:
            self._set_text("")

    def _delete_last(self) -> None:
        self._set_text(self.entry.get_text()[:-1])

    def _entry_changed(self, _entry) -> None:
        self.delete_button.set_visible(bool(self.entry.get_text()))
        self._render_dial()
        self._search_timer.stop()
        self._contacts.begin_contact_search("")
        self._render_suggestions()
        if self._typed:
            self._search_timer.start(SEARCH_DELAY_MS)

    def _search_contacts(self) -> None:
        text = self.entry.get_text().strip()
        request = self._contacts.begin_contact_search(text if len(text) >= 2 else "")
        if request is None:
            self._render_suggestions()
            return

        def found(matches) -> None:
            if self._contacts.apply_contact_results(request, list(matches)):
                self._render_suggestions()

        self._client.find_contacts_async(request.query, found, lambda _message: None)

    def _suggestion_activated(self, _list, row) -> None:
        self._set_text(phone_link_view.dialable(row.address))

    # ---- recent calls ---------------------------------------------------

    def sync(self) -> None:
        """Have the daemon read the iPhone's call history again."""
        if self._syncing or not overview.call_history_enabled(self._status):
            return
        self._syncing = True
        self._render_calls()

        def done(_value) -> None:
            self._syncing = False
            self._calls_read.refresh()
            self._render_calls()

        def failed(message: str) -> None:
            self._syncing = False
            if self._watched:
                self._calls_error = message or _("Call history sync failed")
                self._calls_received = True
            self._render_calls()

        self._client.sync_call_history_async(done, failed)

    def _forget_calls(self) -> None:
        self._calls_read.reset()
        self._calls, self._calls_error, self._calls_received = [], "", False

    def _calls_loaded(self, entries: list[CallHistoryEntry]) -> None:
        self._calls, self._calls_error = list(entries), ""
        self._calls_received = True
        self._render_calls()

    def _calls_failed(self, message: str) -> None:
        self._calls_error = message or _("Call history is unavailable")
        self._calls_received = True
        self._render_calls()

    # ---- rendering ------------------------------------------------------

    def _render(self) -> None:
        hint = self.calls_hint
        self.hint_banner.set_title(hint)
        self.hint_banner.set_revealed(bool(hint))
        # Like the Qt pad: greyed out while calls are off, not just unready.
        enabled = self._status.get("calls_enabled") is True
        for widget in (self.entry, self.suggestions, self.pad):
            widget.set_sensitive(enabled)
        self.delete_button.set_visible(bool(self.entry.get_text()))
        self._render_dial()
        self._render_calls()

    def _render_dial(self) -> None:
        hint = self.calls_hint
        number = phone_link_view.dialable(self.entry.get_text())
        self.call_button.set_sensitive(not hint and bool(number))
        self.call_button.set_tooltip_text(hint or _("Call"))

    def _render_suggestions(self) -> None:
        self.suggestions.remove_all()
        text = self.entry.get_text().strip()
        rows = phone_link_view.dial_suggestions(self._contacts.contact_result_rows())
        for item in rows if len(text) >= 2 else []:
            row = Adw.ActionRow(
                title=item["name"], subtitle=item["address"], activatable=True,
                use_markup=False,
            )
            row.address = item["address"]
            self.suggestions.append(row)
        self.suggestions.set_visible(bool(rows) and len(text) >= 2)

    def _render_calls(self) -> None:
        enabled = overview.call_history_enabled(self._status)
        self.recent_header.set_visible(enabled)
        self.sync_button.set_sensitive(enabled and not self._syncing)
        if not enabled:
            self.recent_off.set_description(
                GLib.markup_escape_text(overview.call_history_hint(self._status))
            )
            self.recent_stack.set_visible_child_name("off")
            return
        child = self.recent_list.get_first_child()
        while child is not None:
            self.recent_list.remove(child)
            child = self.recent_list.get_first_child()
        missed_only = self.missed_filter.get_active()
        groups = overview.call_groups(self._calls[:MAX_CALLS], missed_only=missed_only)
        if self._calls_error or not self._calls_received or not groups:
            if self._calls_error:
                title, text = _("Call history is unavailable"), self._calls_error
            elif not self._calls_received:
                title, text = _("Loading…"), ""
            else:
                title, text = (_("No missed calls") if missed_only else _("No recent calls")), ""
            self.recent_placeholder.set_title(title)
            self.recent_placeholder.set_description(GLib.markup_escape_text(text) or None)
            self.recent_stack.set_visible_child_name("placeholder")
            return
        ready = not self.calls_hint
        group: Adw.PreferencesGroup | None = None
        day = None
        for item in groups:
            if group is None or item["day"] != day:
                day = item["day"]
                group = Adw.PreferencesGroup(title=GLib.markup_escape_text(day))
                self.recent_list.append(group)
            group.add(self._call_row(item, ready))
        self.recent_stack.set_visible_child_name("list")

    def _call_row(self, item: dict, ready: bool) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=item["title"], subtitle=item["detail"], use_markup=False,
        )
        row.update_property([Gtk.AccessibleProperty.LABEL], [
            f"{item['detail']}, {item['caller']}, {item['clock']}",
        ])
        icon = Gtk.Image(icon_name=direction_icon(item["direction"]))
        if item["missed"]:
            row.add_css_class("error")
        row.add_prefix(icon)
        row.add_suffix(Gtk.Label(
            label=item["clock"], css_classes=["dim-label", "numeric"], ellipsize=_END,
        ))
        address = item["address"]
        if address:
            call = _icon_button("call-start-symbolic", _("Call back"), "flat", "circular")
            call.set_sensitive(ready)
            if not ready:
                call.set_tooltip_text(_("Calls are not ready."))
            call.connect("clicked", lambda _b: self._dial(phone_link_view.dialable(address)))
            row.add_suffix(call)
            message = _icon_button(
                "chat-message-new-symbolic", _("Send a message"), "flat", "circular",
            )
            message.connect("clicked", lambda _b: self._compose(address))
            row.add_suffix(message)
        return row
