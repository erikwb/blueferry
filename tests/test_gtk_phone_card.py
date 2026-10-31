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
        "notifications", "close", "volume", "pins", "contacts", "search",
    ],
)
def test_gtk_phone_card(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


CALLS_READY = {
    "daemon": True, "map": True, "calls_enabled": True, "calls_state": "ready",
}


class _Read(Exception):
    """Raised by the blocking reads so :meth:`FakeClient.submit` can name them."""

    def __init__(self, name):
        super().__init__(str(name))
        self.name = name


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

    def notification_apps_async(self, on_ok, on_err=None):
        self.calls.append(("apps", on_ok, on_err))

    def set_notification_pinned_async(self, key, pinned, on_ok, on_err):
        self.calls.append((("pin", key, pinned), on_ok, on_err))

    # TaskRunner for the shared models: the blocking reads name themselves.
    def submit(self, operation, on_done=None, on_error=None, on_finished=None):
        try:
            value = operation()
        except _Read as read:
            self.calls.append((read.name, on_done, on_error))
        except AssertionError:
            self.calls.append(("notifications", on_done, on_error))
        else:  # answered at once (a stale card id)
            if on_done is not None:
                on_done(value)

    def notifications(self, limit):
        raise AssertionError("only called through submit")

    def contact_cards(self, offset, limit, query):
        raise _Read(("cards", offset, query))

    def contact(self, contact_id):
        if contact_id == "gone":
            from blueferry.client import ContactNotFoundError

            raise ContactNotFoundError("unknown contact")
        raise _Read(("card", contact_id))

    def search(self, query, limit):
        raise _Read(("search", query))

    # the plugin parts' workers: nothing runs unless a scenario says so
    def task_runner(self, name, _workers=1):
        from tests.client_fakes import ManualRunner

        return self.runners.setdefault(name, ManualRunner())

    # the Messages tab's reads stay unanswered
    def list_threads_async(self, *_args, **_kwargs):
        pass

    def contact_photo_async(self, *_args, **_kwargs):
        pass

    def mark_thread_read_async(self, *_args, **_kwargs):
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


def _recording(factory):
    """A timer factory that remembers which callback each timer runs."""
    made = []

    def timer(callback):
        made.append((callback, factory(callback)))
        return made[-1][1]

    return timer, made


def _timer_for(made, callback):
    [timer] = [timer for owner, timer in made if owner == callback]
    return timer


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
        "messages", "contacts", "calls", "notifications", "photos"]
    assert [page.get_title() for page in window.stack.get_pages()] == [
        "Messages", "Contacts", "Calls", "Notifications", "Photos"]
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


NOTIFICATIONS_ON = {
    "daemon": True, "map": True, "notification_history_enabled": True,
    "storage_state": "ready",
}


def _exercise_notifications():
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    factory, made = _recording(runner.timer)
    window, client = _window({"daemon": True, "map": True}, timer=factory)
    page = window.notifications
    timer = _timer_for(made, page.history.refresh)
    assert "notifications" not in client.names(), "hidden tab: nothing is read"
    window.show_tab("notifications")
    _settle()
    # Off: nothing is read; why, and the way to the setting.
    assert "notifications" not in client.names()
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
    # Content off: app name and time only, even if a reply carried text.
    assert (row.get_title(), row.get_subtitle()) == ("<b>Chat</b>", "")
    assert page.content_banner.get_revealed()
    assert "Show message content in popups" in page.content_banner.get_title()
    # NotificationsChanged reloads once, after a short delay.
    starts = timer.starts
    client.emit("notifications-changed")
    client.emit("notifications-changed")
    assert timer.active and timer.starts == starts + 2
    assert "notifications" not in client.names()
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
    from blueferry.ui.util import glib_timer

    factory, made = _recording(glib_timer)
    window, client = _window(NOTIFICATIONS_ON, timer=factory)
    window.show_tab("notifications")
    _settle()
    client.take("notifications")[1]({"enabled": True, "content": True, "notifications": []})
    client.emit("notifications-changed")
    timer = _timer_for(made, window.notifications.history.refresh)
    assert timer.active
    window.close()
    _settle()
    assert not timer.active
    client.emit("notifications-changed")
    client.emit("status-invalidated")
    assert not timer.active and not window.phone_card._status_read.pending


def _exercise_volume():
    window, client = _window()
    card = window.phone_card
    down, up, level = card.volume_buttons["down"], card.volume_buttons["up"], card.volume_level
    # Media control off: neither the buttons nor the level.
    assert not down.get_visible() and not up.get_visible() and not level.get_visible()
    client.answer_status({**CALLS_READY, "media_control_enabled": True})
    assert not up.get_visible(), "waiting for the media service"
    client.take("media")[1]({
        "available": True, "player": {"state": "playing", "volume": 0.4},
        "track": {"title": "Song"}, "supported_commands": ["pause", "volume-up"],
    })
    assert down.get_visible() and up.get_visible()
    assert level.get_visible() and level.get_label() == "Volume 40%"
    assert up.get_sensitive() and not down.get_sensitive()
    assert up.get_tooltip_text() == "Turn the iPhone volume up"
    assert down.get_tooltip_text() == (
        "The current iPhone player does not offer volume control")
    # The buttons sit between the transport buttons and the hint.
    order = []
    child = card.media_box.get_first_child()
    while child is not None:
        order.append(child)
        child = child.get_next_sibling()
    assert order.index(card.media_buttons["next"]) < order.index(down) < order.index(
        level) < order.index(up) < order.index(card.media_hint)
    up.emit("clicked")
    client.take(("media-command", "volume-up"))[1](None)
    down.emit("clicked")  # not offered: nothing is sent
    assert ("media-command", "volume-down") not in client.names()
    # A refused step says why and reads the player again.
    toasts = []
    card._toast = toasts.append
    up.emit("clicked")
    client.take(("media-command", "volume-up"))[2]("Not ready")
    assert toasts == ["Not ready"]
    # Commands not known yet: both offered; no level reported: no label.
    client.take("media")[1]({
        "available": True, "player": {"state": "paused"}, "track": {"title": "Song"},
        "supported_commands_known": False,
    })
    assert up.get_sensitive() and down.get_sensitive() and not level.get_visible()
    down.emit("clicked")
    client.take(("media-command", "volume-down"))
    # The media service went away: hidden again.
    client.emit("now-playing-changed")
    client.take("media")[1]({"available": False})
    assert not up.get_visible() and not down.get_visible() and not level.get_visible()
    window.destroy()


def _pin_records(*, removed=True):
    return [
        {"key": "k1", "id": 1, "app_id": "com.example.Mail", "app_name": "Mail",
         "title": "Invoice", "time": "2026-10-26T09:00:00+00:00",
         "pinned": True, "removed": removed, "actionable": False},
        {"key": "k2", "id": 2, "app_id": "com.example.Chat", "app_name": "<b>Chat</b>",
         "title": "Kim", "time": "2026-10-26T10:00:00+00:00",
         "pinned": False, "removed": False, "actionable": True},
        {"app_name": "Old", "time": "2026-10-26T08:00:00+00:00"},  # an older daemon
    ]


def _pin_buttons(row):
    from gi.repository import Gtk

    return [w for w in _descendants(row)
            if isinstance(w, Gtk.Button) and w.get_icon_name() == "view-pin-symbolic"]


def _exercise_pins():
    from blueferry import notification_pin_view as pins
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    status = {
        **NOTIFICATIONS_ON, "notification_muted_app_count": 1,
        "notification_content_shown": True,
    }
    factory, made = _recording(runner.timer)
    window, client = _window(status, timer=factory)
    page = window.notifications
    timer = _timer_for(made, page.history.refresh)
    toasts = []
    page._toast = toasts.append
    assert "apps" not in client.names(), "hidden tab: the muted apps are not read"
    window.show_tab("notifications")
    _settle()
    client.take("apps")[1]({"apps": [
        {"app_id": "com.example.Chat", "app_name": "<b>Chat</b>", "muted": True, "seen": True},
    ], "mute_available": True})
    client.take("notifications")[1]({
        "enabled": True, "content": True, "pins_max": 20, "notifications": _pin_records(),
    })
    mail, chat, old = _rows(page.list)
    # Pinned and removed on the iPhone: marked, dimmed, Unpin offered.
    assert mail.has_css_class("dim-label")
    assert mail.get_subtitle() == "Invoice\nRemoved on iPhone"
    [unpin] = _pin_buttons(mail)
    assert unpin.get_tooltip_text() == pins.UNPIN_TOOLTIP and unpin.has_css_class("accent")
    assert unpin.get_sensitive()
    # A muted app's row is dimmed; it can still be pinned.
    assert chat.has_css_class("dim-label") and chat.get_title() == "<b>Chat</b>"
    [pin] = _pin_buttons(chat)
    assert pin.get_tooltip_text() == pins.PIN_TOOLTIP and not pin.has_css_class("accent")
    # Without a key (older daemon): no pin control, not dimmed.
    assert _pin_buttons(old) == [] and not old.has_css_class("dim-label")

    # Pinning: one call per row at a time, then a read; errors as toasts.
    pin.emit("clicked")
    [pin] = _pin_buttons(_rows(page.list)[1])
    assert not pin.get_sensitive()
    pin.emit("clicked")
    _name, _ok, failed = client.take(("pin", "k2", True))
    assert ("pin", "k2", True) not in client.names()
    failed(pins.FULL_TEXT)
    assert toasts == [pins.FULL_TEXT] and timer.active
    assert _pin_buttons(_rows(page.list)[1])[0].get_sensitive()
    [unpin] = _pin_buttons(_rows(page.list)[0])
    unpin.emit("clicked")
    client.take(("pin", "k1", False))[1](False)
    timer.fire()

    # At the limit: Pin is greyed out with the reason, Unpin still works.
    client.take("notifications")[1]({
        "enabled": True, "content": True, "pins_max": 1,
        "notifications": _pin_records(removed=False),
    })
    mail, chat, _old = _rows(page.list)
    assert not mail.has_css_class("dim-label")
    assert _pin_buttons(mail)[0].get_sensitive()
    [pin] = _pin_buttons(chat)
    assert not pin.get_sensitive() and pin.get_tooltip_text() == pins.pin_limit_text(1)

    # NotificationAppsChanged rereads the muted apps while the tab is shown.
    client.emit("notification-apps-changed")
    client.take("apps")[1]({"apps": [], "mute_available": True})
    assert not _rows(page.list)[1].has_css_class("dim-label")
    # An answer the model drops keeps the pin keys of the rows on screen.
    _check_dropped_answers()
    page.refresh_button.emit("clicked")
    _call, late, _failed = client.take("notifications")
    window.show_tab("messages")
    _settle()
    late({"enabled": True, "content": True, "pins_max": 20, "notifications": [
        {**record, "key": "late"} for record in _pin_records()
    ]})
    assert page._snapshot == {} and page.history.rows == []
    window.show_tab("notifications")
    _settle()
    client.take("apps")[1]({"apps": [], "mute_available": True})
    client.take("notifications")[1]({
        "enabled": True, "content": True, "pins_max": 20, "notifications": _pin_records(),
    })
    _pin_buttons(_rows(page.list)[1])[0].emit("clicked")
    client.take(("pin", "k2", True))[1](True)
    # Hidden: records and muted apps are forgotten, nothing is read.
    window.show_tab("messages")
    _settle()
    assert page._snapshot == {} and page.history.rows == []
    client.emit("notification-apps-changed")
    assert "apps" not in client.names()
    window.destroy()


def _check_dropped_answers():
    """The pin records follow only the answers the shared model takes."""
    from blueferry import notification_pin_view as pins
    from blueferry.ui.notifications import _SnapshotRunner

    class Queue:
        def submit(self, _operation, done, failed, _finished):
            self.done, self.failed = done, failed

    def records(prefix):
        return {"notifications": [
            {**record, "key": f"{prefix}-{record['key']}"} if "key" in record else record
            for record in _pin_records()
        ]}

    def keys():
        return [pins.pin_row_extras(record)["key"] for record in state["snapshot"][
            "notifications"]]

    state = {"rows": ["on screen"], "snapshot": records("shown")}
    queue = Queue()
    runner = _SnapshotRunner(
        queue, rows=lambda: state["rows"], get=lambda: state["snapshot"],
        put=lambda value: state.__setitem__("snapshot", value),
    )
    seen = []

    def accept(_value):
        state["rows"] = ["new rows"]

    # Superseded or unwatched: the model keeps its rows, so the old records stay.
    runner.submit(None, lambda _value: seen.append(keys()))
    queue.done(records("superseded"))
    assert seen == [["superseded-k1", "superseded-k2", ""]], "visible while rendering"
    assert keys() == ["shown-k1", "shown-k2", ""]
    runner.submit(None, None, lambda _message: None)
    queue.failed("Timed out")
    assert keys() == ["shown-k1", "shown-k2", ""]
    # Taken: the new records belong to the new rows.
    runner.submit(None, accept)
    queue.done(records("taken"))
    assert keys() == ["taken-k1", "taken-k2", ""]
    runner.submit(None, None, lambda _message: accept(None))
    queue.failed("Timed out")
    assert state["snapshot"] == {}


CARDS_ON = {**CALLS_READY, "contact_card_count": 2, "contact_card_epoch": 1,
            "search_result_limit": 50}


def _cards_page(offset=0, total=2, query=""):
    cards = [
        {"id": "c1", "name": "<b>Ada</b> Lovelace", "primary": "+15550001",
         "phone_count": 1},
        {"id": "c2", "name": "Bob", "organization": "Example Inc."},
    ]
    return {"contacts": cards[offset:], "total": total, "offset": offset, "query": query}


ADA_CARD = {
    "id": "c1", "name": "<b>Ada</b> Lovelace", "organization": "Analytical",
    "title": "Countess",
    "phones": [{"kind": "mobile", "label": "", "value": "+1 555 0001",
                "number": "+15550001"}],
    "emails": [{"kind": "work", "label": "", "value": "ada@example.org"}],
    "addresses": [{"kind": "home", "label": "", "street": "1 Main St",
                   "locality": "London", "postal_code": "N1"}],
}


def _exercise_contacts():
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    window, client = _window(timer=runner.timer)
    page = window.contacts
    client.answer_status(CARDS_ON)
    assert not [n for n in client.names() if isinstance(n, tuple) and n[0] == "cards"], (
        "hidden tab: nothing is read")
    window.show_tab("contacts")
    _settle()
    client.take(("cards", 0, ""))[1](_cards_page(total=3))
    assert page.list_stack.get_visible_child_name() == "list"
    rows = _rows(page.list)
    # Plain text, never markup.
    assert [row.get_title() for row in rows] == ["<b>Ada</b> Lovelace", "Bob"]
    assert not rows[0].get_use_markup()
    # The avatars show the presenter's initials (contacts_view.initials).
    avatars = [w for row in rows for w in _descendants(row) if type(w).__name__ == "Avatar"]
    assert [a.get_text() for a in avatars] == ["L", "B"]
    assert page.count_label.get_label() == "3 contacts"
    assert page.more_button.get_visible()
    page.more_button.emit("clicked")
    client.take(("cards", 2, ""))[1]({"contacts": [{"id": "c3", "name": "Chloé"}],
                                      "total": 3, "offset": 2, "query": ""})
    assert [row.get_title() for row in _rows(page.list)] == [
        "<b>Ada</b> Lovelace", "Bob", "Chloé"]
    assert _rows(page.list)[0] is rows[0], "a further page keeps the shown rows"
    assert not page.more_button.get_visible()

    # Typing waits for a pause, then reads the first page for the query.
    page.search_entry.set_text("ad")
    assert not [n for n in client.names() if isinstance(n, tuple) and n[0] == "cards"]
    runner.fire_timers()
    client.take(("cards", 0, "ad"))[1]({"contacts": [], "total": 0, "offset": 0,
                                        "query": "ad"})
    assert page.list_stack.get_visible_child_name() == "placeholder"
    assert page.list_placeholder.get_title() == "No Matching Contacts"
    page.search_entry.set_text("")
    runner.fire_timers()
    client.take(("cards", 0, ""))[1](_cards_page())

    # Opening a card shows every detail; Call asks first, Message opens New Message.
    page.list.emit("row-activated", _rows(page.list)[0])
    client.take(("card", "c1"))[1]((ADA_CARD, b""))
    assert page.card_stack.get_visible_child_name() == "card"
    assert page.title_label.get_label() == "<b>Ada</b> Lovelace"
    assert page.avatar.get_text() == "L"
    assert page.subtitle_label.get_label() == "Countess · Analytical"
    details = [(row.get_subtitle(), row.get_title()) for row in _rows(page.sections)]
    assert details == [("Mobile", "+1 555 0001"), ("Work", "ada@example.org"),
                       ("Home", "1 Main St\nN1 London")]
    page.call_button.emit("clicked")
    client.answer_status(CARDS_ON)
    _settle()
    assert "+15550001" in window._dial_dialog.get_body()
    window._dial_dialog.force_close()
    page.message_button.emit("clicked")
    _settle()
    assert window.stack.get_visible_child_name() == "messages"
    assert window.messages._new_recipient.get_text() == "+15550001"
    window.messages._new_message_dialog.force_close()

    # A new epoch reloads the list and reads the open card again; a stale
    # id says so.
    window.show_tab("contacts")
    _settle()
    while any(isinstance(n, tuple) and n[0] == "cards" for n in client.names()):
        client.take(next(n for n in client.names() if isinstance(n, tuple) and n[0] == "cards"))
    page.open_contact("c1")
    client.take(("card", "c1"))[1]((ADA_CARD, b""))
    client.answer_status({**CARDS_ON, "contact_card_epoch": 2})
    client.take(("cards", 0, ""))[1](_cards_page())
    client.take(("card", "c1"))[1]((ADA_CARD, b""))
    page.open_contact("gone")
    assert page.card_banner.get_revealed()
    assert page.card_banner.get_title() == (
        "This contact changed or was removed. Reload the list.")
    assert ("cards", 0, "") in client.names(), "the list is read again"

    # Leaving the tab forgets the cards.
    window.show_tab("messages")
    _settle()
    assert _rows(page.list) == [] and page.book.photo == b""
    window.destroy()


SEARCH_ANSWER = {
    "query": "noon", "limit": 20,
    "conversations": {"total": 1, "results": [
        {"thread_key": "t1", "name": "Ada", "is_group": False}]},
    "messages": {"total": 3, "results": [
        {"thread_key": "t1", "handle": "h9", "thread_name": "Ada", "sender": "Ada",
         "snippet": "<i>at</i> noon", "match_start": 10, "match_length": 4}]},
    "contacts": {"total": 1, "results": [{"id": "c1", "name": "Ada Lovelace"}]},
    "calls": {"total": 1, "results": [
        {"id": "call-1", "direction": "missed", "timestamp": "2026-10-06T14:36:00",
         "address": "+41790000001", "name": "Anna"}]},
}


def _exercise_search():
    from blueferry.ui.search import snippet_markup
    from tests.client_fakes import ManualRunner

    runner = ManualRunner()
    window, client = _window(timer=runner.timer)
    client.answer_status({**CARDS_ON, "call_history_enabled": True})
    search = window.search
    window.activate_action("win.search", None)
    _settle()
    assert search.get_root() is window, "presented over the window"
    search.entry.set_text("noon")
    assert ("search", "noon") not in client.names(), "waits for a pause"
    runner.fire_timers()
    client.take(("search", "noon"))[1](SEARCH_ANSWER)
    rows = _rows(search.groups)
    assert len(rows) == 4
    # Escaped, with only the match in bold.
    assert rows[1].get_subtitle() == "Ada\n&lt;i&gt;at&lt;/i&gt; <b>noon</b>"
    assert snippet_markup("<a> b", (4, 1)) == "&lt;a&gt; <b>b</b>"
    groups = [w.get_title() for w in _descendants(search.groups)
              if type(w).__name__ == "PreferencesGroup"]
    assert groups == ["Conversations", "Messages · 2 more matches", "Contacts", "Calls"]

    # A message hit opens its conversation; closing forgets the query.
    rows[1].emit("activated")
    _settle()
    assert search.entry.get_text() == "" and search.model.query == ""
    assert window.stack.get_visible_child_name() == "messages"
    assert window.messages._focused_handle == "h9"
    assert window.messages._pending_thread_key == "t1"

    # The hit is centred once and stays marked; another conversation forgets it.
    from blueferry.models import Thread

    def thread(key, handles):
        return Thread.from_dict({
            "key": key, "name": key, "is_group": False, "recipients": [f"+4179{key}"],
            "reply_ready": True, "last_ts": "2026-10-06T14:36:00",
            "messages": [{"handle": h, "body": h, "timestamp": "2026-10-06T14:36:00"}
                         for h in handles]})

    messages = window.messages
    messages._apply_threads([thread("t1", ["h1", "h9"]), thread("t2", ["h2"])])
    messages._select_pending_thread()
    _settle()
    assert messages._state.selected_key == "t1"
    assert messages._focused_handle == "h9" and messages._focus_pending is False
    messages.open_thread("t2")
    _settle()
    assert messages._focused_handle == "" and messages._focus_pending is False

    window.open_search_target({"kind": "contact", "contact_id": "c1"})
    _settle()
    assert window.stack.get_visible_child_name() == "contacts"
    assert ("card", "c1") in client.names()

    window.open_search_target({"kind": "call", "call_id": "call-1"})
    _settle()
    assert window.stack.get_visible_child_name() == "calls"
    from blueferry.models import CallHistoryEntry

    client.take(("calls", 200))[1]([CallHistoryEntry.from_dict({
        "direction": "missed", "timestamp": "2026-10-06T14:36:00",
        "address": "+41790000001", "name": "Anna", "id": "call-1"})])
    [row] = _rows(window.calls.recent_list)
    assert row.has_css_class("call-focused")
    assert window.calls._focus_pending is False, "scrolled to once"
    window.calls.missed_filter.set_active(True)
    window.calls.all_filter.set_active(True)
    [row] = _rows(window.calls.recent_list)
    assert row.has_css_class("call-focused"), "a re-render keeps the mark"
    window.show_tab("messages")
    _settle()
    assert window.calls._focused_call == "", "leaving the tab forgets the hit"

    # A daemon without Search says so instead of searching.
    client.answer_status({"daemon": True, "map": True})
    window.open_search()
    _settle()
    search.entry.set_text("noon")
    runner.fire_timers()
    assert ("search", "noon") not in client.names()
    assert search.placeholder.get_title() == "Search Unavailable"
    search.close()
    window.destroy()


if __name__ == "__main__":
    {
        "contacts": _exercise_contacts,
        "search": _exercise_search,
        "volume": _exercise_volume,
        "pins": _exercise_pins,
        "layout": _exercise_layout,
        "card": _exercise_card,
        "dial-pad": _exercise_dial_pad,
        "running-calls": _exercise_running_calls,
        "quick-settings": _exercise_quick_settings,
        "recent-calls": _exercise_recent_calls,
        "notifications": _exercise_notifications,
        "close": _exercise_close,
    }[sys.argv[1]]()
