"""The quick reply box a message popup can open instead of the full window.

A small window with the last few messages of one conversation and a reply
field, pinned at the top centre of the screen when gtk4-layer-shell is
available (see :mod:`blueferry.gtk_layer_shell`). Enter sends; Esc, clicking
elsewhere and an idle timeout close it without sending. The decisions live in
:class:`blueferry.quick_reply_state.QuickReplyState`.
"""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gdk, Gio, Gtk, Pango

from blueferry.client_tasks import TimerFactory
from blueferry.gtk_layer_shell import place_top_center
from blueferry.i18n import _
from blueferry.models import Thread
from blueferry.quick_reply_state import (
    IDLE_CLOSE_SECONDS,
    CloseReason,
    LoadResult,
    QuickReplyState,
    SendAction,
    SendState,
)
from blueferry.ui.util import format_ts, glib_timer

__all__ = ["FADE_OUT_DELAY_MS", "RELOAD_DELAY_MS", "TOP_MARGIN", "WIDTH",
           "Adw", "Gtk", "QuickReplyWindow"]

TOP_MARGIN = 40
WIDTH = 380
RELOAD_DELAY_MS = 500
FADE_OUT_DELAY_MS = 900
FADE_OUT_MS = 200
# After a send fails behind a dismissed box: time for the desktop notice.
LATE_CLOSE_MS = 3000
REVEAL_MS = 220
BUBBLE_CHARS = 30
BUBBLE_LINES = 4

_CSS = """
window.quick-reply-pinned { background: none; }
.quick-reply-card {
  background: @window_bg_color;
  border: 1px solid alpha(@window_fg_color, 0.15);
  border-radius: 14px;
  padding: 12px;
}
.quick-reply-avatar {
  min-width: 30px; min-height: 30px; border-radius: 15px;
  background: @accent_bg_color; color: @accent_fg_color; font-weight: bold;
}
.quick-reply-bubble { padding: 6px 10px; border-radius: 14px; }
.quick-reply-bubble.outgoing { background: @accent_bg_color; color: @accent_fg_color; }
.quick-reply-bubble.failed { background: @error_bg_color; color: @error_fg_color; }
@keyframes quick-reply-pop {
  0% { opacity: 0.25; transform: translateY(26px); background: alpha(@accent_bg_color, 0.45); }
  60% { opacity: 1; transform: translateY(-3px); background: @accent_bg_color; }
  100% { opacity: 1; transform: none; background: @accent_bg_color; }
}
.quick-reply-bubble.sending {
  animation: quick-reply-pop 340ms cubic-bezier(0.25, 0.9, 0.3, 1.0) 1;
}
@keyframes quick-reply-appear { from { opacity: 0; } to { opacity: 1; } }
.quick-reply-status { animation: quick-reply-appear 250ms ease-out 1; }
"""
_css_installed: set[int] = set()


def _install_css() -> None:
    display = Gdk.Display.get_default()
    if display is None or id(display) in _css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _css_installed.add(id(display))


def _bubble(text: str, outgoing: bool) -> Gtk.Label:
    classes = ["card", "msg-bubble", "quick-reply-bubble"]
    classes += ["msg-out", "outgoing"] if outgoing else ["incoming"]
    return Gtk.Label(
        label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
        max_width_chars=BUBBLE_CHARS, lines=BUBBLE_LINES,
        ellipsize=Pango.EllipsizeMode.END, xalign=0,
        halign=Gtk.Align.END if outgoing else Gtk.Align.START,
        css_classes=classes,
    )


class QuickReplyWindow(Gtk.Window):
    def __init__(
        self,
        *,
        application: Gtk.Application | None,
        client,
        handle: str,
        open_in_app: Callable[[str, str], None],
        timer: TimerFactory | None = None,
    ) -> None:
        super().__init__(application=application, title=_("Quick Reply"))
        _install_css()
        self._client = client
        self._open_in_app = open_in_app
        self._state = QuickReplyState(handle)
        make_timer = timer or glib_timer
        self._reload_timer = make_timer(self._load)
        self._fade_timer = make_timer(self._finish_sent)
        self._idle_timer = make_timer(self._idle_elapsed)
        self._late_close_timer = make_timer(self.close)
        self._fade: Adw.TimedAnimation | None = None
        self._marked_read: set[str] = set()
        self._failed_text = ""
        # Dismissed while a send was in flight: finish it out of sight.
        self._hidden_while_sending = False
        self._was_active = False
        self._closing = False
        if place_top_center(self, TOP_MARGIN):
            self.add_css_class("quick-reply-pinned")
        else:
            self.set_resizable(False)
            self.set_default_size(WIDTH, -1)
        self._build()
        self._history_id = client.connect("history-changed", self._history_changed)
        self.connect("notify::is-active", self._active_changed)
        self.connect("close-request", self._close_requested)
        self._load()
        self._restart_idle()

    # ---- layout --------------------------------------------------------

    def _build(self) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       css_classes=["quick-reply-card"])
        card.set_size_request(WIDTH, -1)
        header = Gtk.Box(spacing=9)
        self.avatar_label = Gtk.Label(css_classes=["quick-reply-avatar"])
        self.title_label = Gtk.Label(css_classes=["heading"], xalign=0, hexpand=True,
                                     ellipsize=Pango.EllipsizeMode.END)
        self.time_label = Gtk.Label(css_classes=["caption", "dim-label"])
        for widget in (self.avatar_label, self.title_label, self.time_label):
            header.append(widget)
        card.append(header)
        self._bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.append(self._bubble_box)
        self.status_label = Gtk.Label(css_classes=["quick-reply-status", "caption", "dim-label"],
                                      xalign=1, visible=False)
        card.append(self.status_label)
        self.entry = Gtk.Entry(placeholder_text=_("Reply…"))
        self.entry.connect("activate", self._send)
        self.entry.connect("changed", lambda *_args: self._restart_idle())
        card.append(self.entry)
        self.note_label = Gtk.Label(css_classes=["caption"], wrap=True, xalign=0, visible=False)
        card.append(self.note_label)
        footer = Gtk.Box(spacing=6)
        footer.append(Gtk.Label(label=_("Enter to send · Esc to close"),
                                css_classes=["caption", "dim-label"], xalign=0, hexpand=True))
        self.open_button = Gtk.Button(label=_("Open in BlueFerry"),
                                      css_classes=["flat", "caption"])
        self.open_button.connect(
            "clicked", lambda *_args: self._open_full_window(self.entry.get_text()),
        )
        footer.append(self.open_button)
        card.append(footer)
        self.set_child(card)

        actions = Gio.SimpleActionGroup()
        close = Gio.SimpleAction.new("close", None)
        close.connect("activate", lambda *_args: self._request_close(CloseReason.ESCAPE))
        actions.add_action(close)
        self.insert_action_group("quick-reply", actions)
        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"), Gtk.NamedAction.new("quick-reply.close"),
        ))
        self.add_controller(shortcuts)

    def bubble_labels(self) -> list[Gtk.Label]:
        labels: list[Gtk.Label] = []
        child = self._bubble_box.get_first_child()
        while child is not None:
            label = child.get_child() if isinstance(child, Gtk.Revealer) else child
            if isinstance(label, Gtk.Label):
                labels.append(label)
            child = child.get_next_sibling()
        return labels

    def _render(self, first: bool) -> None:
        state = self._state
        thread = state.thread
        self.title_label.set_label(state.title)
        self.avatar_label.set_label(state.initial)
        newest = thread.messages[-1].timestamp if thread is not None and thread.messages else ""
        self.time_label.set_label(format_ts(newest) if newest else "")
        while (child := self._bubble_box.get_first_child()) is not None:
            self._bubble_box.remove(child)
        for bubble in state.bubbles:
            self._bubble_box.append(_bubble(bubble.text, bubble.outgoing))
        if state.send_state is SendState.FAILED and self._failed_text:
            failed = _bubble(self._failed_text, True)
            failed.add_css_class("failed")
            self._bubble_box.append(failed)
        sending = state.send_state is SendState.SENDING
        self.entry.set_sensitive(state.compose_enabled and not sending)
        self._show_note(state.note)
        if first:
            self.entry.grab_focus()

    def _show_note(self, text: str) -> None:
        self.note_label.set_label(text)
        self.note_label.set_visible(bool(text))

    def _show_status(self, text: str) -> None:
        self.status_label.set_label(text)
        self.status_label.set_visible(True)

    # ---- loading -------------------------------------------------------

    def _load(self) -> None:
        if not self._closing:
            self._client.list_threads_async(self._threads_loaded, self._threads_failed)

    def _threads_loaded(self, threads: list[Thread]) -> None:
        if self._closing:
            return
        first = self._state.thread is None
        result = self._state.apply_threads(threads)
        if result is LoadResult.FOUND:
            self._render(first)
            self._mark_read()
        elif result is LoadResult.RELOAD:
            self._reload_timer.start(RELOAD_DELAY_MS)
        else:
            self._open_full_window()

    def _threads_failed(self, _message: str) -> None:
        if not self._closing and self._state.thread is None:
            self._open_full_window()

    def _history_changed(self, *_args) -> None:
        if self._state.send_state in (SendState.IDLE, SendState.FAILED):
            self._load()

    def _mark_read(self) -> None:
        thread = self._state.thread
        if thread is not None and thread.unread and thread.key not in self._marked_read:
            self._marked_read.add(thread.key)
            self._client.mark_thread_read_async(thread.key)

    def show_handle(self, handle: str) -> None:
        """Another popup was clicked: show its conversation once no send is in flight."""
        self._hidden_while_sending = False
        if self._state.shows_handle(handle):
            # Same conversation: keep what is typed, just refresh.
            self._load()
        elif self._state.request_switch(handle):
            self._reset_for_switch()
        self.present()

    def _reset_for_switch(self) -> None:
        self._fade_timer.stop()
        if self._fade is not None:
            self._fade.reset()
            self._fade = None
        self.set_opacity(1.0)
        self._failed_text = ""
        self.status_label.set_visible(False)
        self.entry.set_text("")
        self._show_note("")
        self._load()

    # ---- sending -------------------------------------------------------

    def _send(self, _entry: Gtk.Entry) -> None:
        self._restart_idle()
        decision = self._state.request_send(self.entry.get_text())
        if decision.action is SendAction.HANDOFF:
            # Confirmations live in the full window; the typed text goes along.
            self._open_full_window(self.entry.get_text())
            return
        if decision.action is SendAction.BLOCKED:
            self._show_note(decision.message)
            return
        if decision.action is not SendAction.SEND or decision.plan is None:
            return
        plan = decision.plan
        if plan.thread is None:
            self._state.send_failed()
            return
        if self._failed_text:
            self._remove_failed_bubble()
        self._show_note("")
        self.status_label.set_visible(False)
        label = _bubble(plan.body, True)
        label.add_css_class("sending")
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                transition_duration=REVEAL_MS, child=label)
        self._bubble_box.append(revealer)
        revealer.set_reveal_child(True)
        self.entry.set_text("")
        self.entry.set_sensitive(False)
        self._client.send_to_thread(
            plan.thread.key, plan.body,
            confirm_group=plan.confirm_group,
            expected_group_token=plan.expected_group_token,
            on_ok=self._sent,
            on_err=lambda _message, body=plan.body: self._send_failed(body),
        )

    def _remove_failed_bubble(self) -> None:
        child = self._bubble_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            label = child.get_child() if isinstance(child, Gtk.Revealer) else child
            if isinstance(label, Gtk.Label) and label.has_css_class("failed"):
                self._bubble_box.remove(child)
            child = following
        self._failed_text = ""

    def _sent(self, _transfer: str) -> None:
        self._state.send_succeeded()
        if self._hidden_while_sending:
            self.close()
            return
        self._show_status(_("Sent"))
        self._fade_timer.start(FADE_OUT_DELAY_MS)

    def _send_failed(self, body: str) -> None:
        self._state.send_failed()
        if self._hidden_while_sending:
            self._report_late_failure(body)
            self._late_close_timer.start(LATE_CLOSE_MS)
            return
        labels = self.bubble_labels()
        if labels:
            labels[-1].remove_css_class("sending")
            labels[-1].add_css_class("failed")
        self._failed_text = body
        self._show_status(_("Not sent"))
        self.entry.set_text(body)
        self.entry.set_sensitive(self._state.compose_enabled)
        self.entry.grab_focus()
        # Messages that arrived while sending were not loaded; catch up.
        self._load()

    def _report_late_failure(self, body: str) -> None:
        """The box was dismissed mid-send and the send failed: say so on the desktop."""
        application = self.get_application()
        if application is None:
            return
        notice = Gio.Notification.new(_("Message not sent"))
        notice.set_body(_("To {name}: {text}").format(name=self._state.title, text=body))
        application.send_notification("blueferry-quick-reply-not-sent", notice)

    def _finish_sent(self) -> None:
        pending = self._state.pending_handle
        if pending and self._state.request_switch(pending):
            self._reset_for_switch()
            return
        self._fade = Adw.TimedAnimation(
            widget=self, value_from=1.0, value_to=0.0, duration=FADE_OUT_MS,
            target=Adw.CallbackAnimationTarget.new(self.set_opacity),
        )
        self._fade.connect("done", lambda *_args: self.close())
        self._fade.play()

    # ---- closing -------------------------------------------------------

    def _open_full_window(self, draft: str = "") -> None:
        self._open_in_app(self._state.handle, draft)
        self._request_close(CloseReason.OPENED_APP)

    def _request_close(self, reason: CloseReason) -> None:
        if self._state.may_close(reason):
            self.close()
        else:
            self._hide_while_sending()

    def _hide_while_sending(self) -> None:
        self._hidden_while_sending = True
        self.set_visible(False)

    def _active_changed(self, *_args) -> None:
        if self.is_active():
            self._was_active = True
        elif self._was_active:
            self._request_close(CloseReason.FOCUS_LOST)

    def _idle_elapsed(self) -> None:
        if self._state.should_auto_close(self.entry.get_text(), IDLE_CLOSE_SECONDS):
            self._request_close(CloseReason.IDLE)
        else:
            self._restart_idle()

    def _restart_idle(self) -> None:
        self._idle_timer.start(IDLE_CLOSE_SECONDS * 1000)

    def _close_requested(self, _window: Gtk.Window) -> bool:
        if not self._state.may_close(CloseReason.ESCAPE):
            self._hide_while_sending()
            return True
        if not self._closing:
            self._closing = True
            for timer in (self._reload_timer, self._fade_timer, self._idle_timer,
                          self._late_close_timer):
                timer.stop()
            if self._history_id:
                self._client.disconnect(self._history_id)
        return False
