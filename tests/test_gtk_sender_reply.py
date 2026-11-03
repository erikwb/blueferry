"""GTK: a reply to a message that may be from a group asks whether it goes to
the sender only, on a private Broadway display."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario
from tests.test_gtk_group_members import FakeClient


@pytest.mark.parametrize("scenario", ["cancel", "send", "stale"])
def test_gtk_sender_only_reply(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


ALICE = {
    "key": "address:phone:15551111111", "name": "Alice", "recipients": ["+15551111111"],
    "reply_ready": False, "group_ambiguous": True, "sender_reply_token": "sender:one",
    "last_ts": "2026-10-31T18:00:00Z",
    "messages": [{
        "handle": "1", "body": "ok", "timestamp": "2026-10-31T18:00:00Z",
        "outgoing": False, "read": True, "address": "+15551111111",
    }],
}


class SendClient(FakeClient):
    def send_to_thread(self, thread_key, body, *, confirm_group, expected_group_token,
                       on_ok, on_err):
        self.calls.append(
            (("send", thread_key, body, confirm_group, expected_group_token), on_ok, on_err),
        )


def _exercise(scenario):
    from blueferry.models import Thread
    from blueferry.ui.conversations import Adw, ConversationsPage, Gtk
    from tests.test_gtk_phone_card import _settle

    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    client = SendClient()
    toasts = []
    page = ConversationsPage(client, toasts.append)
    window = Adw.Window(content=page, default_width=900, default_height=700)
    window.present()
    _settle()
    try:
        client.take("threads")[1]([Thread.from_dict(ALICE)])
        page._thread_list.select_row(page._thread_list.get_row_at_index(0))
        _settle()
        # The reply box stays open; Send asks first.
        assert page._entry.get_sensitive() and page._send_btn.get_sensitive()
        page._entry.set_text("just you")
        page._on_send(None)
        _settle()
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog)
        assert dialog.get_heading() == "Reply to Alice only?"
        assert "+15551111111" in dialog.get_body()
        assert not [name for name in client.names() if name[0] == "send"]

        if scenario == "cancel":
            dialog.emit("response", "cancel")
            _settle()
            assert page._entry.get_text() == "just you"
            assert not [name for name in client.names() if name[0] == "send"]
            return

        if scenario == "stale":
            # Another message that may be from a group arrives meanwhile.
            page._reload_threads()
            client.take("threads")[1]([
                Thread.from_dict({**ALICE, "sender_reply_token": "sender:two"}),
            ])
            _settle()
            dialog.emit("response", "send")
            _settle()
            assert not [name for name in client.names() if name[0] == "send"]
            assert toasts and "new message" in toasts[-1]
            return

        dialog.emit("response", "send")
        _settle()
        client.take(("send", ALICE["key"], "just you", True, "sender:one"))
        assert toasts == []
    finally:
        window.close()
        _settle()


if __name__ == "__main__":
    _exercise(sys.argv[1])
