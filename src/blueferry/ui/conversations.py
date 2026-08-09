"""Conversations page — SMS/iMessage threads, read history + send replies.

History, group correlation, and reply recipients come from the daemon.
Live signals tell this page when to refresh that backend-owned model.
"""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk, Pango

from blueferry.i18n import _
from blueferry.ui.util import format_ts

_ELLIPSIZE_END = Pango.EllipsizeMode.END


class ConversationsPage(Gtk.Box):
    def __init__(self, client, toast) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            hexpand=True,
            vexpand=True,
        )
        self._client = client
        self._toast = toast
        self._threads: dict[str, dict] = {}
        self._current: str | None = None
        self._confirmed_groups: set[str] = set()
        self._reload_pending = False
        self._reload_again = False

        # ---- left: thread list ----------------------------------------
        self._thread_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._thread_selected_handler = self._thread_list.connect(
            "row-selected", self._on_thread_selected
        )
        sidebar_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=240, child=self._thread_list
        )
        sidebar_page = Adw.NavigationPage(
            child=sidebar_scroll,
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
        conversation_header.append(self.back_button)
        conversation_header.append(self._conversation_title)
        right.append(conversation_header)
        right.append(Gtk.Separator())
        right.append(self._stack)

        compose = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        self._entry = Gtk.Entry(
            placeholder_text=_("Write a Message"), hexpand=True, sensitive=False
        )
        self._entry.connect("activate", self._on_send)
        self._send_btn = Gtk.Button(
            icon_name="document-send-symbolic",
            sensitive=False,
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

        self._load_history()
        client.connect("history-changed", self._on_history_changed)

    # ---- data ----------------------------------------------------------

    def _load_history(self) -> None:
        self._reload_threads()

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

    def _apply_threads(self, loaded) -> bool:
        selected_handle = None
        if self._current in self._threads:
            messages = self._threads[self._current]["messages"]
            if messages:
                selected_handle = messages[-1].get("handle")

        self._threads = {}
        for thread in loaded:
            current = thread.to_dict()
            key = str(current.get("key") or "")
            if not key:
                continue
            messages = []
            for message in current.get("messages", []):
                messages.append(
                    {
                        **message,
                        "ts": str(message.get("timestamp") or ""),
                    }
                )
            self._threads[key] = {**current, "key": key, "messages": messages}

        if self._current not in self._threads:
            self._current = None
            if selected_handle:
                for key, thread in self._threads.items():
                    if any(
                        message.get("handle") == selected_handle for message in thread["messages"]
                    ):
                        self._current = key
                        break
        self._rebuild_thread_list()
        if self._current in self._threads:
            self._msg_list.remove_all()
            for message in self._threads[self._current]["messages"]:
                self._append_bubble(message)
            can_reply = bool(self._threads[self._current].get("reply_ready", True))
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._stack.set_visible_child_name("messages")
            self._scroll_to_bottom()
        else:
            self._entry.set_sensitive(False)
            self._send_btn.set_sensitive(False)
            self._stack.set_visible_child_name("empty")
        self._reload_finished()
        return False

    # ---- thread list ---------------------------------------------------

    def _rebuild_thread_list(self) -> None:
        selected = self._current
        # Removing/recreating rows changes the ListBox selection. Without
        # blocking this handler, re-selecting the current row synchronously
        # redraws all its messages; _ingest() then appends the new message a
        # second time. User-initiated selections remain unblocked.
        self._thread_list.handler_block(self._thread_selected_handler)
        try:
            self._thread_list.remove_all()
            order = sorted(self._threads.values(), key=lambda t: t.get("last_ts", ""), reverse=True)
            for thread in order:
                row = Gtk.ListBoxRow()
                row.thread_key = thread["key"]
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
                        label=thread["name"],
                        xalign=0,
                        css_classes=["heading"],
                        ellipsize=_ELLIPSIZE_END,
                    )
                )
                last = thread["messages"][-1]["body"] if thread["messages"] else ""
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
                if thread["key"] == selected:
                    self._thread_list.select_row(row)
        finally:
            self._thread_list.handler_unblock(self._thread_selected_handler)

    def _on_thread_selected(self, _list, row) -> None:
        if row is None:
            return
        self._current = row.thread_key
        thread = self._threads.get(self._current)
        can_reply = bool(thread and thread.get("reply_ready", True))
        self._entry.set_sensitive(can_reply)
        self._send_btn.set_sensitive(can_reply)
        self._stack.set_visible_child_name("messages")
        self._conversation_title.set_label(thread["name"])
        self.split_view.set_show_content(True)
        self._msg_list.remove_all()
        for msg in thread["messages"]:
            self._append_bubble(msg)
        self._scroll_to_bottom()

    # ---- message bubbles ----------------------------------------------

    def _append_bubble(self, msg: dict) -> None:
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
        bubble.set_halign(Gtk.Align.END if msg["outgoing"] else Gtk.Align.START)
        if msg["outgoing"]:
            bubble.add_css_class("msg-out")
        body = Gtk.Label(
            label=msg["body"], xalign=0, wrap=True, selectable=True, max_width_chars=46
        )
        bubble.append(body)
        ts = format_ts(msg["ts"])
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
        if not body or self._current is None:
            return
        thread = self._threads[self._current]
        if thread.get("is_group") and thread["key"] not in self._confirmed_groups:
            self._confirm_group_send(thread, body)
            return
        self._dispatch_send(thread, body, confirm_group=False)

    def _confirm_group_send(self, thread: dict, body: str) -> None:
        recipients = "\n".join(f"• {value}" for value in thread["recipients"])
        dialog = Adw.AlertDialog(
            heading=_("Reply to {name}?").format(name=thread["name"]),
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
                self._confirmed_groups.add(thread["key"])
                self._dispatch_send(thread, body, confirm_group=True)

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _dispatch_send(
        self,
        thread: dict,
        body: str,
        *,
        confirm_group: bool,
    ) -> None:
        self._entry.set_sensitive(False)
        self._send_btn.set_sensitive(False)

        def done(_transfer: str) -> None:
            # The daemon persists the outgoing event before emitting its
            # content-free HistoryChanged invalidation. Avoid an optimistic
            # append so the same bubble cannot appear twice.
            self._entry.set_text("")
            can_reply = bool(thread.get("reply_ready", True))
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._entry.grab_focus()

        def failed(text: str) -> None:
            can_reply = bool(thread.get("reply_ready", True))
            self._entry.set_sensitive(can_reply)
            self._send_btn.set_sensitive(can_reply)
            self._toast(_("Send failed: {error}").format(error=text))

        if not thread.get("reply_ready"):
            failed(_("This thread has no unambiguous reply destination"))
            return
        self._client.send_to_thread(
            thread["key"],
            body,
            confirm_group=confirm_group,
            on_ok=done,
            on_err=failed,
        )

    # ---- live ----------------------------------------------------------

    def _on_history_changed(self, _client, _revision: dict) -> None:
        self._reload_threads()
