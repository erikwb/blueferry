"""GTK: the quick reply box a message popup can open, on a private Broadway display."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario

SCENARIOS = [
    "render", "send-once", "sent-waits", "failed", "escape", "second-click",
    "long-text", "read-only", "fallback", "incoming",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_gtk_quick_reply(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


KEY = "address:phone:15551111111"
LONG = "a" * 400 + " 🙂🙂🙂"


def _message(handle, body, *, outgoing=False, read=False):
    return {
        "handle": handle, "body": body, "timestamp": "2026-11-10T18:00:00Z",
        "outgoing": outgoing, "read": read, "address": "+15551111111",
    }


def _thread(messages, *, key=KEY, name="Jordan", reply_ready=True):
    return {
        "key": key, "name": name, "recipients": ["+15551111111"],
        "reply_ready": reply_ready, "last_ts": "2026-11-10T18:00:00Z", "messages": messages,
    }


SIX = [_message(f"h{n}", f"m{n}", outgoing=n % 2 == 1, read=n < 6) for n in range(1, 7)]
OTHER = _thread([_message("o1", "other one")], key="address:phone:15552222222", name="Casey")


class FakeTimer:
    def __init__(self, callback):
        self.callback = callback
        self.delay = None

    def start(self, delay_ms):
        self.delay = delay_ms

    def stop(self):
        self.delay = None

    def fire(self):
        assert self.delay is not None, "timer not armed"
        self.delay = None
        self.callback()


class FakeClient:
    """Queues every daemon call; the scenario answers them when it chooses."""

    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.read = []

    def connect(self, signal, handler, *args):
        self.handlers[signal] = handler
        return len(self.handlers)

    def disconnect(self, _handler_id):
        pass

    def mark_thread_read_async(self, thread_key, *_args, **_kwargs):
        self.read.append(thread_key)

    def list_threads_async(self, on_ok, on_err=None, limit=1000):
        self.calls.append(("threads", on_ok, on_err))

    def send_to_thread(self, thread_key, body, *, confirm_group, expected_group_token,
                       on_ok, on_err):
        self.calls.append(
            (("send", thread_key, body, confirm_group, expected_group_token), on_ok, on_err),
        )

    def names(self):
        return [call[0] for call in self.calls]

    def sends(self):
        return [name for name in self.names() if isinstance(name, tuple) and name[0] == "send"]

    def take(self, name):
        names = self.names()
        assert name in names, (name, names)
        return self.calls.pop(names.index(name))


def _exercise(scenario):
    from blueferry.models import Thread
    from blueferry.ui.quick_reply import (
        FADE_OUT_DELAY_MS,
        RELOAD_DELAY_MS,
        WIDTH,
        Adw,
        Gtk,
        QuickReplyWindow,
    )
    from tests.test_gtk_phone_card import _settle

    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    client = FakeClient()
    timers = []
    opened = []

    def timer(callback):
        made = FakeTimer(callback)
        timers.append(made)
        return made

    def fire(delay):
        due = [made for made in timers if made.delay == delay]
        assert len(due) == 1, [made.delay for made in timers]
        due[0].fire()
        _settle()

    def answer(threads):
        client.take("threads")[1]([Thread.from_dict(value) for value in threads])
        _settle()

    def bubbles(window):
        return [label.get_label() for label in window.bubble_labels()]

    handle = "h6"
    window = QuickReplyWindow(
        application=None, client=client, handle=handle, open_in_app=opened.append, timer=timer,
    )
    window.present()
    _settle()
    try:
        if scenario == "fallback":
            answer([OTHER])
            assert window.get_visible() and opened == []
            fire(RELOAD_DELAY_MS)
            answer([OTHER])
            assert opened == [handle]
            assert not window.get_visible()
            return

        reply_ready = scenario != "read-only"
        messages = [*SIX[:-1], _message("h6", LONG)] if scenario == "long-text" else SIX
        answer([_thread(messages, reply_ready=reply_ready), OTHER])
        entry = window.entry

        if scenario == "render":
            assert bubbles(window) == ["m3", "m4", "m5", "m6"]
            assert window.title_label.get_label() == "Jordan"
            assert window.get_focus() is entry.get_delegate() or window.get_focus() is entry
            assert client.read == [KEY]
            return

        if scenario == "long-text":
            assert window.get_width() <= WIDTH, window.get_width()
            return

        if scenario == "read-only":
            assert not entry.get_sensitive()
            assert window.note_label.get_visible()
            assert window.note_label.get_label() == "This conversation is read-only"
            entry.set_text("hi")
            entry.emit("activate")
            _settle()
            assert client.sends() == []
            return

        if scenario == "incoming":
            client.handlers["history-changed"](client, {})
            answer([_thread([*SIX, _message("h7", "m7")]), OTHER])
            assert bubbles(window) == ["m4", "m5", "m6", "m7"]
            return

        entry.set_text("on my way")
        if scenario == "escape":
            window.activate_action("quick-reply.close", None)
            _settle()
            assert client.sends() == []
            assert not window.get_visible()
            return

        entry.emit("activate")
        entry.emit("activate")
        _settle()
        assert client.sends() == [("send", KEY, "on my way", False, "")]
        assert bubbles(window)[-1] == "on my way"
        assert entry.get_text() == ""

        if scenario == "send-once":
            return

        if scenario == "second-click":
            window.show_handle("o1")
            _settle()
            assert window.title_label.get_label() == "Jordan"
            assert "threads" not in client.names()
            client.take(client.sends()[0])[1]("transfer")
            _settle()
            assert window.status_label.get_label() == "Sent"
            fire(FADE_OUT_DELAY_MS)
            answer([_thread(SIX), OTHER])
            assert window.get_visible()
            assert window.title_label.get_label() == "Casey"
            assert bubbles(window) == ["other one"]
            return

        send = client.take(client.sends()[0])
        if scenario == "sent-waits":
            assert not window.status_label.get_visible()
            window.activate_action("quick-reply.close", None)  # deferred while sending
            _settle()
            assert window.get_visible()
            send[1]("transfer")
            _settle()
            assert window.status_label.get_visible()
            assert window.status_label.get_label() == "Sent"
            fire(FADE_OUT_DELAY_MS)
            assert not window.get_visible()
            return

        if scenario == "failed":
            send[2]("iPhone not reachable")
            _settle()
            assert window.status_label.get_label() == "Not sent"
            assert window.bubble_labels()[-1].has_css_class("failed")
            assert entry.get_text() == "on my way"
            assert entry.get_sensitive()
            assert window.get_visible()
            return
        raise AssertionError(f"unknown scenario {scenario}")
    finally:
        window.close()
        _settle()


if __name__ == "__main__":
    _exercise(sys.argv[1])
