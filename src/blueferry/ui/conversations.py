"""Conversations page — SMS/iMessage threads, read history + send replies.

History, group correlation, and reply recipients come from the daemon.
Live signals tell this page when to refresh that backend-owned model.
"""

from __future__ import annotations

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
    ReplyPlan,
)
from blueferry.i18n import _
from blueferry.models import BackendStatus, Thread, ThreadMessage
from blueferry.ui.status_presenter import (
    map_connection_refused,
    map_connection_refused_message,
)
from blueferry.ui.util import format_ts

_ELLIPSIZE_END = Pango.EllipsizeMode.END


class MessageComposer(Gtk.ScrolledWindow):
    """A wrapping message editor that grows before it starts scrolling."""

    def __init__(self, placeholder: str, on_submit) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            min_content_height=44,
            max_content_height=150,
            propagate_natural_height=True,
            hexpand=True,
        )
        self._view = Gtk.TextView(
            accepts_tab=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
        )
        self._placeholder = Gtk.Label(
            label=placeholder,
            css_classes=["dim-label"],
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
            margin_top=8,
            margin_start=10,
        )
        self._placeholder.set_can_target(False)
        overlay = Gtk.Overlay(child=self._view)
        overlay.add_overlay(self._placeholder)
        self.set_child(overlay)
        self._on_submit = on_submit
        self._view.get_buffer().connect("changed", self._content_changed)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self._view.add_controller(keys)

    def _content_changed(self, buffer) -> None:
        self._placeholder.set_visible(buffer.get_char_count() == 0)
        self.queue_resize()

    def _key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        if enter and not state & Gdk.ModifierType.SHIFT_MASK:
            self._on_submit(self)
            return True
        return False

    def connect_changed(self, callback) -> int:
        return self._view.get_buffer().connect("changed", callback)

    def get_text(self) -> str:
        buffer = self._view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def set_text(self, value: str) -> None:
        self._view.get_buffer().set_text(value)

    def grab_focus(self) -> bool:
        return self._view.grab_focus()


def _participant_lines(value: str) -> list[str]:
    """Return unique non-empty addresses from the one-per-line editor."""
    result: list[str] = []
    for line in value.splitlines():
        address = line.strip()
        if address and address not in result:
            result.append(address)
    return result


def _group_roster_banner_title(thread: Thread) -> str:
    """Describe why this named group currently needs roster review."""
    if thread.roster_changed:
        sender = thread.unexpected_sender or _("Someone new")
        template = _(
            "{sender} is not in BlueFerry's saved participant list for "
            "{group}. Review the list before replying."
        )
    else:
        sender = thread.prompt_sender or _("Someone")
        template = _(
            "{sender} has sent a message to the group {group}. "
            "BlueFerry needs its participant list before you can reply."
        )
    return template.format(sender=sender, group=thread.name)


class ConversationsPage(Gtk.Box):
    def __init__(self, client, toast) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self._client = client
        self._toast = toast
        self._state = ConversationState(select_first=False)
        self._pending_open_handle: str | None = None
        self._reload_pending = False
        self._reload_again = False
        self._new_destination: str | None = None
        self._map_refused_banner = Adw.Banner(
            title=map_connection_refused_message(),
        )
        self._map_refused_banner.set_revealed(False)
        self.append(self._map_refused_banner)

        # ---- left: thread list ----------------------------------------
        self._thread_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._thread_selected_handler = self._thread_list.connect(
            "row-selected", self._on_thread_selected
        )
        sidebar_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            width_request=240,
            hexpand=True,
            vexpand=True,
            child=self._thread_list,
        )
        sidebar_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=10,
            margin_end=6,
        )
        sidebar_header.append(
            Gtk.Label(
                label=_("Conversations"),
                css_classes=["heading"],
                hexpand=True,
                xalign=0,
            )
        )
        self._new_message_button = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text=_("New Message"),
            css_classes=["flat"],
        )
        self._new_message_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("New Message")]
        )
        self._new_message_button.connect("clicked", self._open_new_message)
        sidebar_header.append(self._new_message_button)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.append(sidebar_header)
        sidebar.append(Gtk.Separator())
        sidebar.append(sidebar_scroll)
        sidebar_page = Adw.NavigationPage(
            child=sidebar,
            title=_("Conversations"),
        )

        # ---- right: message view + compose ----------------------------
        right = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            hexpand=True,
            vexpand=True,
        )
        self._msg_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            css_classes=["background"],
            hexpand=True,
        )
        self._msg_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            hexpand=True,
            vexpand=True,
            child=self._msg_list,
        )
        self._placeholder = Gtk.Label(
            label=_("Select a Conversation"), css_classes=["dim-label", "title-2"], vexpand=True
        )
        self._stack = Gtk.Stack(hexpand=True, vexpand=True)
        self._stack.add_named(self._placeholder, "empty")
        self._stack.add_named(self._msg_scroll, "messages")
        conversation_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self.back_button = Gtk.Button(
            icon_name="go-previous-symbolic",
            tooltip_text=_("Back to Conversations"),
            visible=False,
        )
        self.back_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Back to Conversations")]
        )
        self.back_button.connect("clicked", lambda _button: self.split_view.set_show_content(False))
        self._conversation_title = Gtk.Label(
            label=_("Messages"),
            css_classes=["heading"],
            hexpand=True,
            xalign=0,
            ellipsize=_ELLIPSIZE_END,
        )
        self._group_roster_button = Gtk.Button(
            icon_name="system-users-symbolic",
            tooltip_text=_("Edit Group Participants"),
            css_classes=["flat"],
            visible=False,
        )
        self._group_roster_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Edit Group Participants")]
        )
        self._group_roster_button.connect(
            "clicked", self._open_group_roster_dialog
        )
        conversation_header.append(self.back_button)
        conversation_header.append(self._conversation_title)
        conversation_header.append(self._group_roster_button)
        right.append(conversation_header)
        right.append(Gtk.Separator())
        self._group_roster_banner = Adw.Banner()
        self._group_roster_banner.set_button_label(_("Add Participants"))
        self._group_roster_banner.set_revealed(False)
        self._group_roster_banner.connect(
            "button-clicked", self._open_group_roster_dialog
        )
        right.append(self._group_roster_banner)
        right.append(self._stack)

        compose = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._entry = MessageComposer(
            _("Write a Message"), self._on_send
        )
        self._entry.set_sensitive(False)
        self._send_btn = Gtk.Button(
            icon_name="document-send-symbolic",
            sensitive=False,
            valign=Gtk.Align.END,
            css_classes=["suggested-action"],
            tooltip_text=_("Send Message"),
        )
        self._send_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Send Message")])
        self._send_btn.connect("clicked", self._on_send)
        compose.append(self._entry)
        compose.append(self._send_btn)
        right.append(Gtk.Separator())
        right.append(compose)
        content_page = Adw.NavigationPage(child=right, title=_("Messages"))
        self.split_view = Adw.NavigationSplitView(
            sidebar=sidebar_page,
            content=content_page,
            min_sidebar_width=220,
            max_sidebar_width=320,
            hexpand=True,
            vexpand=True,
        )
        self.append(self.split_view)

        self._build_new_message_dialog()

        self._reload_threads()
        client.connect("history-changed", self._on_history_changed)
        client.connect("status-invalidated", self._on_status_invalidated)
        self._refresh_status()

    def _build_new_message_dialog(self) -> None:
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )

        content.append(Gtk.Label(label=_("To"), xalign=0, css_classes=["heading"]))
        self._new_recipient = Gtk.SearchEntry(
            placeholder_text=_("Contact, phone number, or email address"),
        )
        self._new_recipient.connect("search-changed", self._on_contact_search_changed)
        self._new_recipient.connect("activate", lambda _entry: self._new_body.grab_focus())
        content.append(self._new_recipient)

        self._contact_results = Gtk.ListBox(css_classes=["boxed-list"])
        self._contact_results.connect("row-activated", self._on_contact_selected)
        content.append(
            Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                min_content_height=150,
                max_content_height=220,
                propagate_natural_height=True,
                child=self._contact_results,
            )
        )

        content.append(Gtk.Label(label=_("Message"), xalign=0, css_classes=["heading"]))
        self._new_body = MessageComposer(
            _("Write a Message"), self._send_new_message
        )
        self._new_body.connect_changed(
            lambda _buffer: self._update_new_send_button()
        )
        content.append(self._new_body)

        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda _button: self._new_message_dialog.close())
        self._new_send_button = Gtk.Button(
            label=_("Send"),
            css_classes=["suggested-action"],
            sensitive=False,
        )
        self._new_send_button.connect("clicked", self._send_new_message)
        header = Adw.HeaderBar(
            title_widget=Adw.WindowTitle(title=_("New Message")),
        )
        header.pack_start(cancel)
        header.pack_end(self._new_send_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        self._new_message_dialog = Adw.Dialog(
            title=_("New Message"),
            content_width=440,
            follows_content_size=True,
            child=toolbar,
        )

    # ---- data ----------------------------------------------------------

    def open_message(self, handle: str) -> None:
        """Select a notification's message, refreshing if it is not loaded."""
        self._pending_open_handle = handle
        if not self._select_pending_message():
            self._reload_threads()

    def _select_pending_message(self) -> bool:
        handle = self._pending_open_handle
        if not handle:
            return False
        if not self._state.select_message(handle):
            return False

        row = self._thread_list.get_first_child()
        while (
            row is not None
            and getattr(row, "thread_key", None) != self._state.selected_key
        ):
            row = row.get_next_sibling()
        if row is None:
            return False
        self._pending_open_handle = None
        if self._thread_list.get_selected_row() is row:
            self._on_thread_selected(self._thread_list, row)
        else:
            self._thread_list.select_row(row)
        return True

    def _reload_threads(self) -> None:
        """Refresh the canonical backend model while preserving selection."""
        if self._reload_pending:
            self._reload_again = True
            return
        self._reload_pending = True
        self._client.list_threads_async(self._apply_threads, self._reload_failed)

    def _reload_failed(self, _message: str) -> bool:
        self._reload_finished()
        return False

    def _reload_finished(self) -> None:
        self._reload_pending = False
        if self._reload_again:
            self._reload_again = False
            self._reload_threads()

    def _refresh_status(self) -> None:
        self._client.get_status_async(self._apply_status, self._status_failed)

    def _apply_status(self, status: BackendStatus) -> bool:
        self._state.apply_snapshot(ConversationSnapshot(status, None))
        self._map_refused_banner.set_revealed(
            map_connection_refused(status.to_dict())
        )
        return False

    def _status_failed(self, _message: str) -> bool:
        self._map_refused_banner.set_revealed(False)
        return False

    def _on_status_invalidated(self, _client) -> None:
        self._refresh_status()

    # ---- new message ---------------------------------------------------

    def _open_new_message(self, _button) -> None:
        self._new_destination = None
        self._state.begin_contact_search("")
        self._new_recipient.set_text("")
        self._new_body.set_text("")
        self._contact_results.remove_all()
        self._update_new_send_button()
        self._new_message_dialog.present(self.get_root())
        self._new_recipient.grab_focus()

    def _on_contact_search_changed(self, entry) -> None:
        query = entry.get_text().strip()
        self._new_destination = None
        self._update_new_send_button()
        request = self._state.begin_contact_search(query)
        self._contact_results.remove_all()
        if request is None:
            return

        def apply(matches) -> None:
            if not self._state.apply_contact_results(request, list(matches)):
                return
            self._contact_results.remove_all()
            for name, address in self._state.contact_results:
                row = Gtk.ListBoxRow()
                row.contact_address = address
                item = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=2,
                    margin_top=6,
                    margin_bottom=6,
                    margin_start=10,
                    margin_end=10,
                )
                item.append(
                    Gtk.Label(
                        label=name,
                        xalign=0,
                        ellipsize=_ELLIPSIZE_END,
                    )
                )
                display_address = address if "@" in address else f"+{address}"
                item.append(
                    Gtk.Label(
                        label=display_address,
                        xalign=0,
                        ellipsize=_ELLIPSIZE_END,
                        css_classes=["dim-label", "caption"],
                    )
                )
                row.set_child(item)
                self._contact_results.append(row)

        self._client.find_contacts_async(
            request.query,
            apply,
            lambda _message: None,
        )

    def _on_contact_selected(self, _list, row) -> None:
        address = str(row.contact_address)
        self._new_recipient.set_text(address)
        self._new_destination = address
        self._state.begin_contact_search("")
        self._contact_results.remove_all()
        self._update_new_send_button()
        self._new_body.grab_focus()

    def _update_new_send_button(self) -> None:
        if not hasattr(self, "_new_send_button"):
            return
        self._new_send_button.set_sensitive(
            bool(self._new_recipient.get_text().strip() and self._new_body.get_text().strip())
        )

    def _send_new_message(self, _widget) -> None:
        recipient = self._new_destination or self._new_recipient.get_text().strip()
        body = self._new_body.get_text().strip()
        if not recipient or not body:
            return
        self._new_recipient.set_sensitive(False)
        self._new_body.set_sensitive(False)
        self._new_send_button.set_sensitive(False)

        def done(_transfer: str) -> None:
            self._new_recipient.set_sensitive(True)
            self._new_body.set_sensitive(True)
            self._new_message_dialog.close()

        def failed(text: str) -> None:
            self._new_recipient.set_sensitive(True)
            self._new_body.set_sensitive(True)
            self._update_new_send_button()
            self._toast(_("Send failed: {error}").format(error=text))

        self._client.send_message(recipient, body, done, failed)

    def _apply_threads(self, loaded) -> bool:
        threads = tuple(
            thread
            for thread in loaded
            if isinstance(thread, Thread) and thread.key
        )
        self._state.apply_snapshot(ConversationSnapshot(None, threads))
        self._rebuild_thread_list()
        current = self._state.selected
        if current is not None:
            self._msg_list.remove_all()
            for message in current.messages:
                self._append_bubble(message, is_group=current.is_group)
            can_reply = current.reply_ready
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._update_group_roster_banner(current)
            self._stack.set_visible_child_name("messages")
            self._scroll_to_bottom()
        else:
            self._entry.set_sensitive(False)
            self._send_btn.set_sensitive(False)
            self._update_group_roster_banner(None)
            self._stack.set_visible_child_name("empty")
        self._reload_finished()
        self._select_pending_message()
        self._maybe_warn_roster_change()
        return False

    # ---- thread list ---------------------------------------------------

    def _rebuild_thread_list(self) -> None:
        selected = self._state.selected_key
        # Removing/recreating rows changes the ListBox selection. Without
        # blocking this handler, re-selecting the current row synchronously
        # redraws the conversation during the list rebuild. User-initiated
        # selections remain unblocked.
        self._thread_list.handler_block(self._thread_selected_handler)
        try:
            self._thread_list.remove_all()
            order = sorted(
                self._state.threads,
                key=lambda thread: thread.last_ts,
                reverse=True,
            )
            for thread in order:
                row = Gtk.ListBoxRow()
                row.thread_key = thread.key
                box = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=2,
                    margin_top=8,
                    margin_bottom=8,
                    margin_start=10,
                    margin_end=10,
                )
                box.append(
                    Gtk.Label(
                        label=thread.name,
                        xalign=0,
                        css_classes=["heading"],
                        ellipsize=_ELLIPSIZE_END,
                    )
                )
                last = thread.messages[-1].body if thread.messages else ""
                box.append(
                    Gtk.Label(
                        label=last.replace("\n", " "),
                        xalign=0,
                        ellipsize=_ELLIPSIZE_END,
                        css_classes=["dim-label"],
                    )
                )
                row.set_child(box)
                self._thread_list.append(row)
                if thread.key == selected:
                    self._thread_list.select_row(row)
        finally:
            self._thread_list.handler_unblock(self._thread_selected_handler)

    def _on_thread_selected(self, _list, row) -> None:
        if row is None:
            return
        self._state.selected_key = row.thread_key
        thread = self._state.selected
        if thread is None:
            return
        can_reply = thread.reply_ready
        self._entry.set_sensitive(can_reply)
        self._send_btn.set_sensitive(can_reply)
        self._update_group_roster_banner(thread)
        self._stack.set_visible_child_name("messages")
        self._conversation_title.set_label(thread.name)
        self.split_view.set_show_content(True)
        self._msg_list.remove_all()
        for message in thread.messages:
            self._append_bubble(message, is_group=thread.is_group)
        self._scroll_to_bottom()

    def _update_group_roster_banner(self, thread: Thread | None) -> None:
        required = bool(thread and thread.participants_required)
        self._group_roster_button.set_visible(
            bool(thread and thread.group_origin == "named")
        )
        self._group_roster_banner.set_revealed(required)
        if not required or thread is None:
            return
        self._group_roster_banner.set_title(_group_roster_banner_title(thread))

    def _maybe_warn_roster_change(self) -> None:
        if self.get_root() is None:
            return
        thread = self._state.next_roster_warning()
        if thread is None:
            return
        sender = thread.unexpected_sender or _("Someone new")
        dialog = Adw.AlertDialog(
            heading=_("Group Membership May Have Changed"),
            body=_(
                "{sender} sent a message to {group}, but is not in BlueFerry's "
                "saved participant list. Replies are disabled until you review "
                "the list. This can also happen if you have multiple groups "
                "named {group}, because BlueFerry cannot distinguish them."
            ).format(sender=sender, group=thread.name),
        )
        dialog.add_response("later", _("Not Now"))
        dialog.add_response("review", _("Review Participants"))
        dialog.set_default_response("review")
        dialog.set_response_appearance(
            "review", Adw.ResponseAppearance.SUGGESTED
        )

        def response(_dialog, selected: str) -> None:
            if selected == "review":
                self._open_group_roster_dialog(None, thread)

        dialog.connect("response", response)
        dialog.present(self.get_root())

    def _open_group_roster_dialog(
        self, _source, selected_thread: Thread | None = None,
    ) -> None:
        thread = selected_thread or self._state.selected
        if not thread or thread.group_origin != "named":
            return
        sender = thread.prompt_sender or _("Someone")
        dialog = Adw.AlertDialog(
            heading=_("Who is in {group}?").format(group=thread.name),
            body=_(
                "{sender} has sent a message to a group named {group}, which you're "
                "a member of. BlueFerry can't determine the participants of "
                "this group chat, but if you fill in the members, it can work.\n\n"
                "Enter every other participant's phone number or Apple ID email, "
                "one per line. Changing this list only updates BlueFerry's local "
                "understanding of the group; it does not add or remove anyone in "
                "Messages on your iPhone.\n\nBlueFerry identifies named groups by "
                "name. If you have multiple groups named {group}, BlueFerry may "
                "combine them and use the wrong participant list. The list can "
                "also become outdated if the group is renamed or its membership "
                "changes."
            ).format(sender=sender, group=thread.name),
        )
        editor = Gtk.TextView(
            accepts_tab=False,
            monospace=True,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
            wrap_mode=Gtk.WrapMode.NONE,
        )
        editor.get_buffer().set_text("\n".join(thread.recipients))
        dialog.set_extra_child(
            Gtk.ScrolledWindow(
                min_content_height=120,
                max_content_height=220,
                child=editor,
            )
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save Participants"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("save")
        dialog.set_response_appearance(
            "save", Adw.ResponseAppearance.SUGGESTED
        )

        def response(_dialog, selected: str) -> None:
            if selected != "save":
                return
            buffer = editor.get_buffer()
            recipients = _participant_lines(
                buffer.get_text(
                    buffer.get_start_iter(), buffer.get_end_iter(), False
                )
            )

            def saved(updated: Thread) -> None:
                self._state.group_participants_saved(updated)
                self._reload_threads()

            self._client.set_group_participants_async(
                thread.key,
                recipients,
                saved,
                lambda error: self._toast(
                    _("Could not save participants: {error}").format(error=error)
                ),
            )

        dialog.connect("response", response)
        dialog.present(self.get_root())

    # ---- message bubbles ----------------------------------------------

    def _append_bubble(self, message: ThreadMessage, *, is_group: bool) -> None:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=3,
            margin_bottom=3,
            margin_start=8,
            margin_end=8,
        )
        bubble = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["card", "msg-bubble"]
        )
        bubble.set_halign(
            Gtk.Align.END if message.outgoing else Gtk.Align.START
        )
        if message.outgoing:
            bubble.add_css_class("msg-out")
        if is_group:
            bubble.append(
                Gtk.Label(
                    label=_("You") if message.outgoing else message.sender,
                    xalign=0,
                    wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                    max_width_chars=46,
                    css_classes=["dim-label", "caption", "heading"],
                )
            )
        body = Gtk.Label(
            label=message.body,
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            max_width_chars=46,
        )
        bubble.append(body)
        ts = format_ts(message.timestamp)
        if ts:
            bubble.append(Gtk.Label(label=ts, xalign=1, css_classes=["dim-label", "caption"]))
        outer.append(bubble)
        row.set_child(outer)
        self._msg_list.append(row)

    def _scroll_to_bottom(self) -> None:
        def _scroll() -> bool:
            adj = self._msg_scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False

        GLib.idle_add(_scroll)

    # ---- send ----------------------------------------------------------

    def _on_send(self, _widget) -> None:
        body = self._entry.get_text().strip()
        plan = self._state.plan_reply(body)
        if (
            plan.disposition is ReplyDisposition.CONFIRM_GROUP
            and plan.thread is not None
        ):
            self._confirm_group_send(plan.thread, plan.body)
            return
        if plan.ready:
            self._dispatch_send(plan)

    def _confirm_group_send(self, thread: Thread, body: str) -> None:
        recipients = "\n".join(f"• {value}" for value in thread.recipients)
        dialog = Adw.AlertDialog(
            heading=_("Reply to {name}?").format(name=thread.name),
            body=_(
                "The iPhone identifies this group by this participant set:\n\n{recipients}"
            ).format(recipients=recipients),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("send", _("Send"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("send")
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)

        def responded(_dialog, response: str) -> None:
            if response == "send":
                plan = self._state.plan_reply(
                    body,
                    thread_key=thread.key,
                    confirm_group=True,
                )
                if plan.ready:
                    self._dispatch_send(plan)

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _dispatch_send(self, plan: ReplyPlan) -> None:
        if not plan.ready or plan.thread is None:
            return
        thread = plan.thread
        self._entry.set_sensitive(False)
        self._send_btn.set_sensitive(False)

        def done(_transfer: str) -> None:
            # The daemon persists the outgoing event before emitting its
            # content-free HistoryChanged invalidation. Avoid an optimistic
            # append so the same bubble cannot appear twice.
            self._entry.set_text("")
            self._state.reply_sent(plan, preserve_selection=True)
            can_reply = thread.reply_ready
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._entry.grab_focus()

        def failed(text: str) -> None:
            can_reply = thread.reply_ready
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._toast(_("Send failed: {error}").format(error=text))

        self._client.send_to_thread(
            thread.key,
            plan.body,
            confirm_group=plan.confirm_group,
            on_ok=done,
            on_err=failed,
        )

    # ---- live ----------------------------------------------------------

    def _on_history_changed(self, _client, _revision: dict) -> None:
        self._reload_threads()
