"""The GTK main window as a phone card on a private Broadway display: the
card, the Messages/Calls/Notifications tabs and when they read."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize(
    "scenario", [
        "layout", "card", "quick-settings", "dial-pad", "running-calls", "recent-calls",
        "notifications", "close",
    ],
)
def test_gtk_phone_card(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


CALLS_READY = {
    "daemon": True, "map": True, "calls_enabled": True, "calls_state": "ready",
}


class FakeClient:
    """Queues every daemon call; the scenario answers them when it chooses."""

    available = True

    def __init__(self, status=None):
        from blueferry.models import BackendStatus

        self.status = BackendStatus.from_dict(status or {"daemon": True, "map": True})
        self.calls = []
        self.handlers = {}
        self.runners = {}
        self.opened = []  # what the window would have launched

    # GObject signals
    def connect(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)
        return 0

    def emit(self, name, *args):
        for handler in self.handlers.get(name, []):
            handler(self, *args)

    # reads the scenarios answer later
    def get_status_async(self, on_ok, on_err=None):
        self.calls.append(("status", on_ok, on_err))

    def now_playing_async(self, on_ok, on_err=None):
        self.calls.append(("media", on_ok, on_err))

    def call_history_async(self, limit, on_ok, on_err=None):
        self.calls.append((("calls", limit), on_ok, on_err))

    def sync_call_history_async(self, on_ok, on_err):
        self.calls.append(("sync", on_ok, on_err))

    def find_contacts_async(self, query, on_ok, on_err=None):
        self.calls.append((("contacts", query), on_ok, on_err))

    def send_media_command_async(self, command, on_ok, on_err):
        self.calls.append((("media-command", command), on_ok, on_err))

    def dial_async(self, number, on_ok, on_err):
        self.calls.append((("dial", number), on_ok, on_err))

    def calls_async(self, on_ok, on_err=None):
        self.calls.append(("list-calls", on_ok, on_err))

    def answer_call_async(self, call_id, on_ok, on_err):
        self.calls.append((("answer", call_id), on_ok, on_err))

    def hangup_call_async(self, call_id, on_ok, on_err):
        self.calls.append((("hangup", call_id), on_ok, on_err))

    def swap_calls_async(self, on_ok, on_err):
        self.calls.append(("swap", on_ok, on_err))

    def set_phone_audio_route_async(self, route, on_ok, on_err):
        self.calls.append((("route", route), on_ok, on_err))

    def set_proximity_lock_async(self, enabled, grace, on_ok, on_err):
        self.calls.append((("proximity", enabled, grace), on_ok, on_err))

    # TaskRunner for NotificationHistory
    def submit(self, operation, on_done=None, on_error=None, on_finished=None):
        self.calls.append(("notifications", on_done, on_error))

    def notifications(self, limit):
        raise AssertionError("only called through submit")

    # the plugin parts' workers: nothing runs unless a scenario says so
    def task_runner(self, name, _workers=1):
        from tests.client_fakes import ManualRunner

        return self.runners.setdefault(name, ManualRunner())

    # the Messages tab's reads stay unanswered
    def list_threads_async(self, *_args, **_kwargs):
        pass

    def contact_photo_async(self, *_args, **_kwargs):
        pass

    def names(self):
        return [call[0] for call in self.calls]

    def take(self, name):
        names = self.names()
        assert name in names, (name, names)
        return self.calls.pop(names.index(name))

    def answer_status(self, values):
        """StatusChanged, and every status read answered with ``values``."""
        from blueferry.models import BackendStatus

        self.emit("status-invalidated")
        while "status" in self.names():
            self.take("status")[1](BackendStatus.from_dict(values))


class FakeSettings:
    opened: ClassVar[list] = []

    def __init__(self, _client):
        from types import SimpleNamespace

        self.listeners = []
        self.plugins = SimpleNamespace(add_listener=self.listeners.append)

    def open(self, _parent, category=""):
        self.opened.append(category)

    def close(self):
        pass


def _window(status=None, *, width=940, timer=None):
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry.setup_client import ConfigurationState
    from blueferry.ui import window as window_module

    Adw, Gtk = window_module.Adw, window_module.Gtk

    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    configuration = ConfigurationState.from_dict({"configured": True, "saved": True})
    client = FakeClient()
    FakeSettings.opened = []
    with (
        patch.object(window_module, "SetupClient",
                     lambda: SimpleNamespace(configuration=lambda: configuration)),
        patch.object(window_module, "SettingsDialog", FakeSettings),
    ):
        window = window_module.MainWindow(
            None, client, name_loader=lambda done: done("<b>Test</b> iPhone"),
            timer=timer, opener=client.opened.append, open_file=client.opened.append,
            files=lambda *_args: None,
        )
    window.set_default_size(width, 700)
    window.present()
    _settle()
    if status is not None:
        client.answer_status(status)
    return window, client


def _settle():
    import time

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
            if isinstance(w, Gtk.Label) and w.get_mapped()]


def _rows(widget):
    from gi.repository import Adw

    return [w for w in _descendants(widget) if isinstance(w, Adw.ActionRow)]


def _exercise_layout():
    from blueferry.ui.window import Adw, Gtk

    window, _client = _window(CALLS_READY)
    assert [page.get_name() for page in window.stack.get_pages()] == [
        "messages", "calls", "notifications", "photos"]
    assert [page.get_title() for page in window.stack.get_pages()] == [
        "Messages", "Calls", "Notifications", "Photos"]
    assert window.header.get_title_widget() is window.switcher
    assert not window.switcher_bar.get_reveal()
    assert window.stack.get_visible_child_name() == "messages"
    # The old iPhone dialog is gone; the card replaces it.
    assert window.lookup_action("phone-overview") is None
    assert window.lookup_action("phone") is not None
    card = window.phone_card
    assert card.name_label.get_label() == "<b>Test</b> iPhone"  # verbatim, no markup
    assert card.connection_label.get_label() == "Connected"
    assert card.get_orientation() == Gtk.Orientation.HORIZONTAL
    window.destroy()

    # Narrow: the switcher moves to the bottom and the card stacks its parts.
    window, _client = _window(CALLS_READY, width=420)
    assert window.switcher_bar.get_reveal()
    assert isinstance(window.header.get_title_widget(), Adw.WindowTitle)
    assert card is not window.phone_card
    assert window.phone_card.get_orientation() == Gtk.Orientation.VERTICAL
    assert window.calls.columns.get_orientation() == Gtk.Orientation.VERTICAL
    assert window.messages.split_view.get_collapsed()
    window.destroy()


def _exercise_card():
    window, client = _window()
    card = window.phone_card
    # Without phone calls: the hint names the setting instead of values.
    client.answer_status({"daemon": True, "map": False})
    assert card.connection_label.get_label() == "Not connected"
    assert not card.status_chips.get_visible() and card.status_hint.get_visible()
    assert "Settings \u203a Calls & Audio" in card.status_hint.get_label()
    assert card.media_hint.get_visible() and not card.media_title.get_visible()
    assert "Show and control what the iPhone is playing" in card.media_hint.get_label()
    assert "media" not in client.names(), "media control is off: nothing is read"

    client.status_values = {
        **CALLS_READY, "media_control_enabled": True,
        "phone_battery_level": 57, "phone_battery_source": "ble",
        "phone_signal_strength": 80, "phone_network_name": "<i>Sunrise</i>",
        "phone_network_status": "roaming",
    }
    client.emit("status-invalidated")
    before = client.names().count("status")
    client.emit("status-invalidated")  # the card coalesces it into its read
    # One more read, the Messages tab's own; the card waits for its first.
    assert client.names().count("status") == before + 1 and card._status_read.pending
    client.answer_status(client.status_values)
    assert card.status_chips.get_visible() and not card.status_hint.get_visible()
    assert [label for label in _labels(card.status_chips)] == [
        "57 %", "80 %", "<i>Sunrise</i> (roaming)"]
    assert "Battery 57 %" in card.status_chips.get_tooltip_text()
    assert card.media_hint.get_label() == "Waiting for the iPhone's media service."
    client.take("media")[1]({
        "available": True, "player": {"state": "playing"},
        "track": {"title": "<b>Song</b>", "artist": "A & B"},
        "supported_commands": ["pause", "next"],
    })
    assert card.media_title.get_visible()
    assert card.media_title.get_label() == "<b>Song</b> — A & B"
    buttons = card.media_buttons
    assert [buttons[n].get_sensitive() for n in ("previous", "toggle", "next")] == [
        False, True, True]
    assert buttons["toggle"].get_icon_name() == "media-playback-pause-symbolic"
    # A refused command says why and reads the player again.
    toasts = []
    window.toast = toasts.append
    card._toast = toasts.append
    buttons["next"].emit("clicked")
    client.take(("media-command", "next"))[2]("Not supported")
    assert toasts == ["Not supported"] and "media" in client.names()
    client.take("media")

    # A hidden window reads nothing and forgets the track.
    window.set_visible(False)
    _settle()
    client.emit("status-invalidated")
    client.emit("now-playing-changed")
    assert "media" not in client.names() and not card._status_read.pending
    assert not card.media_title.get_visible()
    window.present()
    _settle()
    assert card._status_read.pending
    window.destroy()


def _exercise_dial_pad():
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    window, client = _window({"daemon": True, "map": True}, timer=runner.timer)
    calls = window.calls
    window.show_tab("calls")
    _settle()
    # Calls are off: the banner says where to turn them on, the pad is grey.
    assert calls.hint_banner.get_revealed()
    assert "Enable phone calls through this computer" in calls.hint_banner.get_title()
    assert not calls.pad.get_sensitive() and not calls.call_button.get_sensitive()
    calls.hint_banner.emit("button-clicked")
    assert FakeSettings.opened == ["calls"]

    client.answer_status({**CALLS_READY, "calls_state": "connecting"})
    assert calls.pad.get_sensitive() and "online" in calls.hint_banner.get_title()
    client.answer_status(CALLS_READY)
    assert not calls.hint_banner.get_revealed()
    for key in ("0", "7", "9", "#"):
        calls.keys[key].emit("clicked")
    calls.append_key("x")
    assert calls.entry.get_text() == "079#"
    assert runner.timers[0].delay is None, "the pad does not search contacts"
    calls.delete_button.emit("clicked")
    assert calls.entry.get_text() == "079"
    # A long press: on 0 it enters "+", on Delete it clears the field.
    from blueferry.ui.calls import Gtk

    calls._long_press(Gtk.GestureLongPress(), "+")
    assert calls.entry.get_text() == "079+"
    calls._long_press(Gtk.GestureLongPress(), "")
    assert calls.entry.get_text() == ""
    calls.append_key("0")
    calls.append_key("7")
    calls.append_key("9")
    assert calls.call_button.get_sensitive()

    # Typed letters search the contacts; numbers are offered as stored.
    calls.entry.set_text("Kim")
    runner.fire_timers()
    _call, found, _failed = client.take(("contacts", "Kim"))
    found([("Pat US", "+15550100123"), ("Kim AU", "0491570156"), ("Mail", "kim@example.org")])
    rows = _rows(calls.suggestions)
    assert [(row.get_title(), row.get_subtitle()) for row in rows] == [
        ("Pat US", "+15550100123"), ("Kim AU", "0491570156")]
    calls.suggestions.emit("row-activated", rows[1])
    assert calls.entry.get_text() == "0491570156"
    assert not calls.suggestions.get_visible()
    assert not any(t.active for t in runner.timers), "choosing does not search again"

    # Calling goes through the confirmation dialog, which dials on Call.
    calls.call_button.emit("clicked")
    client.answer_status(CALLS_READY)
    _settle()
    dialog = window._dial_dialog
    assert dialog is not None and "0491570156" in dialog.get_body()
    dialog.emit("response", "call")
    assert ("dial", "0491570156") in client.names()
    # Pressing Enter in the field asks too.
    calls.entry.emit("activate")
    client.answer_status(CALLS_READY)
    _settle()
    assert window._dial_dialog is not None
    window._dial_dialog.force_close()
    window.destroy()


def _exercise_running_calls():
    from blueferry.models import CallsSnapshot
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    now = [1_000_000.0]
    window, client = _window(timer=runner.timer)
    running = window.calls.running
    running._clock = lambda: now[0]
    # Calls off: ListCalls is not even asked.
    client.answer_status({"daemon": True, "map": True})
    assert "list-calls" not in client.names() and not running.get_visible()
    # Read while the window is shown, whichever tab is visible.
    client.answer_status(CALLS_READY)
    from datetime import datetime, timezone
    started = datetime.fromtimestamp(now[0] - 65, timezone.utc).isoformat()
    ringing = {"call_id": "voicecall01", "state": "incoming", "number": "+41790000001",
               "contact_name": "<b>Kim</b>", "first_seen": started}
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": [ringing]}))
    assert running.get_visible()
    [(row, _call, avatar, buttons)] = running.rows
    assert (row.get_title(), row.get_subtitle()) == ("<b>Kim</b>", "Incoming call…")
    assert [b.get_tooltip_text() for b in buttons] == ["Answer", "Decline"]
    assert avatar.get_text() == "<b>Kim</b>"
    assert running.sound.row.get_parent() is running  # the sound switch under the calls
    buttons[0].emit("clicked")
    assert not buttons[1].get_sensitive(), "no second action while one runs"
    buttons[1].emit("clicked")
    assert ("hangup", "voicecall01") not in client.names()
    client.take(("answer", "voicecall01"))[1](None)
    # Answered: the call ticks while the Calls tab is visible.
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": [
        {**ringing, "state": "active"}]}))
    [(row, _call, _avatar, buttons)] = running.rows
    assert row.get_subtitle() == "1:05"
    assert [b.get_tooltip_text() for b in buttons] == ["Hold", "Hang Up"]
    ticker = running._ticker
    assert not ticker.active, "the duration ticks only on the visible Calls tab"
    window.show_tab("calls")
    _settle()
    assert ticker.active and ticker.delay == 1000
    now[0] += 10
    ticker.fire()
    assert row.get_subtitle() == "1:15" and ticker.active
    buttons[0].emit("clicked")
    client.take("swap")[2]("Not supported")
    assert "list-calls" in client.names()
    client.take("list-calls")[2]("Timed out")
    # A failed ListCalls: no stale call, and the pad says calls are unavailable.
    assert not running.get_visible() and not ticker.active
    assert running.state == "unavailable"
    assert "oFono" in window.calls.hint_banner.get_title()
    # CallsChanged reads again; a hidden window does not.
    client.emit("calls-changed")
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": [
        {**ringing, "state": "held"}]}))
    assert running.rows[0][0].get_subtitle() == "On hold"
    assert running.rows[0][3][0].get_tooltip_text() == "Resume"
    running.rows[0][3][1].emit("clicked")
    client.take(("hangup", "voicecall01"))[1](None)
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": []}))
    assert not running.get_visible() and running.rows == []
    window.set_visible(False)
    _settle()
    client.emit("calls-changed")
    assert "list-calls" not in client.names()
    window.destroy()


def _exercise_quick_settings():
    window, client = _window()
    card = window.phone_card
    assert card.quick_settings.get_tooltip_text() == "Quick Settings"
    sound, lock = card.sound.row, card.away_lock.row
    # An older daemon: both switches greyed out, the reason underneath.
    client.answer_status({"daemon": True, "map": True})
    assert not sound.get_sensitive() and not lock.get_sensitive()
    assert "Update the BlueFerry backend" in lock.get_subtitle()
    client.answer_status({
        "daemon": True, "map": True, "phone_audio_route": "phone",
        "proximity_lock": "present",
        "proximity_lock_enabled": False, "proximity_lock_grace_sec": 30,
    })
    assert lock.get_sensitive() and not lock.get_active() and lock.get_subtitle() == ""
    lock.set_active(True)
    assert not lock.get_sensitive()
    client.take(("proximity", True, 30))[1]({})
    assert lock.get_active() and lock.get_sensitive()
    assert sound.get_sensitive() and not sound.get_active()
    assert sound.get_subtitle() == "iPhone sound plays on the iPhone."
    toasts = []
    card.sound._toast = toasts.append
    sound.set_active(True)
    assert not sound.get_sensitive()
    client.take(("route", "pc"))[2]("Bluetooth audio is not connected")
    assert not sound.get_active() and sound.get_sensitive()  # the daemon's report again
    assert toasts == ["Bluetooth audio is not connected"]
    # The popover opens from the card.
    card.quick_settings.popup()
    _settle()
    assert card.quick_settings.get_popover().get_visible()
    card.quick_settings.popdown()
    window.destroy()


def _entries():
    from blueferry.models import CallHistoryEntry

    return [
        CallHistoryEntry("missed", "2026-10-06T14:36:00", "+41790000001", "<b>Anna</b>"),
        CallHistoryEntry("missed", "2026-10-06T14:30:00", "+41790000001", "<b>Anna</b>"),
        CallHistoryEntry("outgoing", "2026-10-05T09:00:00", "+41790000002", None),
    ]


def _exercise_recent_calls():
    window, client = _window(CALLS_READY)
    calls = window.calls
    # Off: the tab says so, links the setting and reads nothing.
    window.show_tab("calls")
    _settle()
    assert calls.recent_stack.get_visible_child_name() == "off"
    assert "recent calls\u201d in Settings \u203a Calls" in calls.recent_off.get_description()
    assert not any(isinstance(n, tuple) and n[0] == "calls" for n in client.names())

    window.show_tab("messages")
    _settle()
    client.answer_status({**CALLS_READY, "call_history_enabled": True})
    assert ("calls", 200) not in client.names(), "nothing is read while the tab is hidden"
    window.show_tab("calls")
    _settle()
    assert calls.recent_placeholder.get_title() == "Loading…"
    client.emit("call-history-changed")  # coalesced into one more read
    client.take(("calls", 200))[1](_entries())
    client.take(("calls", 200))[1](_entries())
    assert calls.recent_stack.get_visible_child_name() == "list"
    rows = _rows(calls.recent_list)
    assert [(row.get_title(), row.get_subtitle()) for row in rows] == [
        ("<b>Anna</b> (2)", "Missed · +41790000001"), ("+41790000002", "Outgoing")]
    # Another StatusChanged with the same opt-in neither reads nor rebuilds.
    client.answer_status({**CALLS_READY, "call_history_enabled": True, "phone_battery_level": 9})
    assert ("calls", 200) not in client.names()
    assert _rows(calls.recent_list)[0] is rows[0]

    # Readiness from a ListCalls answer: the call-back buttons follow it,
    # and an answer that changes nothing does not rebuild the rows.
    from blueferry.models import CallsSnapshot

    def call_back_sensitive():
        return [b.get_sensitive() for b in _descendants(calls.recent_list)
                if isinstance(b, type(calls.call_button)) and b.get_tooltip_text() in (
                    "Call back", "Calls are not ready.")]

    while "list-calls" in client.names():
        client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": []}))
    rows = _rows(calls.recent_list)
    assert all(call_back_sensitive())
    client.emit("calls-changed")
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": []}))
    assert _rows(calls.recent_list)[0] is rows[0]
    client.emit("calls-changed")
    client.take("list-calls")[2]("Timed out")
    assert calls.hint_banner.get_revealed() and not calls.call_button.get_sensitive()
    assert call_back_sensitive() and not any(call_back_sensitive())
    client.emit("calls-changed")
    client.take("list-calls")[1](CallsSnapshot.from_dict({"state": "ready", "calls": []}))
    assert all(call_back_sensitive())
    assert rows[0].has_css_class("error")
    calls.missed_filter.set_active(True)
    assert [row.get_title() for row in _rows(calls.recent_list)] == ["<b>Anna</b> (2)"]

    # Call back asks first; Message opens New Message for the number.
    buttons = {b.get_tooltip_text(): b for b in _descendants(_rows(calls.recent_list)[0])
               if hasattr(b, "get_icon_name") and b.get_tooltip_text()}
    buttons["Call back"].emit("clicked")
    client.answer_status({**CALLS_READY, "call_history_enabled": True})
    _settle()
    assert "+41790000001" in window._dial_dialog.get_body()
    window._dial_dialog.force_close()
    buttons["Send a message"].emit("clicked")
    _settle()
    assert window.stack.get_visible_child_name() == "messages"
    assert window.messages._new_recipient.get_text() == "+41790000001"
    window.messages._new_message_dialog.force_close()

    # Refresh from iPhone syncs, then reads the list again.
    window.show_tab("calls")
    _settle()
    while ("calls", 200) in client.names():  # stale reads are ignored
        client.take(("calls", 200))[1](_entries())
    assert len(_rows(calls.recent_list)) == 1
    calls.sync_button.emit("clicked")
    assert not calls.sync_button.get_sensitive()
    client.take("sync")[1](2)
    client.take(("calls", 200))[2]("Timed out")
    assert calls.recent_placeholder.get_title() == "Call history is unavailable"
    assert calls.recent_placeholder.get_description() == "Timed out"
    # Leaving the tab forgets the list; a late reply is ignored.
    client.emit("call-history-changed")
    window.show_tab("messages")
    _settle()
    client.take(("calls", 200))[1](_entries())
    assert calls._calls == [] and calls.recent_placeholder.get_title() == "Loading…"
    window.destroy()


NOTIFICATIONS_ON = {"daemon": True, "map": True, "notification_history_enabled": True}


def _exercise_notifications():
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    window, client = _window({"daemon": True, "map": True}, timer=runner.timer)
    page = window.notifications
    timer = page.history._timer
    assert "notifications" not in client.names(), "hidden tab: nothing is read"
    window.show_tab("notifications")
    _settle()
    _call, done, _failed = client.take("notifications")
    done({"enabled": False, "notifications": []})
    # Off: why, and the way to the setting.
    assert page.stack.get_visible_child_name() == "placeholder"
    assert page.placeholder.get_title() == "Notification List Is Off"
    assert "Keep recent iPhone notifications" in page.placeholder.get_description()
    assert page.settings_button.get_visible() and not page.refresh_button.get_sensitive()
    page.settings_button.emit("clicked")
    assert FakeSettings.opened == ["notifications"]

    client.answer_status(NOTIFICATIONS_ON)
    client.take("notifications")[1]({"enabled": True, "content": False, "notifications": [
        {"app_name": "<b>Chat</b>", "title": "Kim", "subtitle": "Group", "body": "",
         "time": "2026-10-26T10:00:00+00:00"},
    ]})
    assert page.stack.get_visible_child_name() == "list"
    [row] = _rows(page.list)
    assert (row.get_title(), row.get_subtitle()) == ("<b>Chat</b>", "Kim · Group")
    assert page.content_banner.get_revealed()
    assert "Show message content in popups" in page.content_banner.get_title()
    # NotificationsChanged reloads once, after a short delay.
    client.emit("notifications-changed")
    client.emit("notifications-changed")
    assert timer.active and timer.starts == 2 and "notifications" not in client.names()
    timer.fire()
    client.take("notifications")[2]("Access denied")
    assert page.placeholder.get_title() == "Notifications Are Unavailable"
    assert page.placeholder.get_description() == "Access denied"
    # Another tab: forgotten, the pending reload cancelled, nothing re-armed.
    client.emit("notifications-changed")
    assert timer.active
    window.show_tab("calls")
    _settle()
    assert not timer.active and page.history.rows == []
    client.emit("notifications-changed")
    assert not timer.active
    # Back on the tab of a hidden window: still nothing.
    window.set_visible(False)
    window.show_tab("notifications")
    _settle()
    assert "notifications" not in client.names()
    window.present()
    _settle()
    assert "notifications" in client.names()
    window.destroy()


def _exercise_close():
    # With the real GLib timer: closing the window removes the armed source.
    window, client = _window(NOTIFICATIONS_ON)
    window.show_tab("notifications")
    _settle()
    client.take("notifications")[1]({"enabled": True, "content": True, "notifications": []})
    client.emit("notifications-changed")
    timer = window.notifications.history._timer
    assert timer.active
    window.close()
    _settle()
    assert not timer.active
    client.emit("notifications-changed")
    client.emit("status-invalidated")
    assert not timer.active and not window.phone_card._status_read.pending


if __name__ == "__main__":
    {
        "layout": _exercise_layout,
        "card": _exercise_card,
        "dial-pad": _exercise_dial_pad,
        "running-calls": _exercise_running_calls,
        "quick-settings": _exercise_quick_settings,
        "recent-calls": _exercise_recent_calls,
        "notifications": _exercise_notifications,
        "close": _exercise_close,
    }[sys.argv[1]]()
