"""GTK: a reply to a group with unconfirmed members asks who is in it first,
on a private Broadway display."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize("scenario", ["cancel", "save"])
def test_gtk_group_members_dialog(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


GROUP = {
    "key": "group:participants:son|wife",
    "name": "Son, Wife",
    "is_group": True,
    "recipients": ["+15551111111"],
    "observed_recipients": ["+15551111111"],
    "missing_members": ["Son"],
    "reply_ready": False,
    "participants_required": True,
    "group_origin": "unnamed",
    "prompt_sender": "Wife",
    "last_ts": "2026-10-31T18:00:00Z",
    "messages": [{
        "handle": "1", "body": "dinner at 7", "timestamp": "2026-10-31T18:00:00Z",
        "outgoing": False, "read": True, "sender": "Wife", "address": "+15551111111",
    }],
}


class FakeClient:
    """Queues every daemon call; the scenario answers them when it chooses."""

    def __init__(self):
        self.calls = []

    def connect(self, *_args):
        return 0

    def get_status_async(self, *_args):
        pass

    def contact_photo_async(self, *_args):
        pass

    def mark_thread_read_async(self, *_args, **_kwargs):
        pass

    def list_threads_async(self, on_ok, on_err=None, limit=1000):
        self.calls.append(("threads", on_ok, on_err))

    def find_contacts_async(self, query, on_ok, on_err=None):
        self.calls.append((("contacts", query), on_ok, on_err))

    def set_group_participants_async(self, thread_key, recipients, on_ok, on_err=None):
        self.calls.append((("participants", thread_key, tuple(recipients)), on_ok, on_err))

    def send_to_thread(self, thread_key, body, *, confirm_group, expected_group_token,
                       on_ok, on_err):
        self.calls.append((("send", thread_key, body, confirm_group), on_ok, on_err))

    def names(self):
        return [call[0] for call in self.calls]

    def take(self, name):
        names = self.names()
        assert name in names, (name, names)
        return self.calls.pop(names.index(name))


def _exercise(scenario):
    from blueferry.models import Thread
    from blueferry.ui.conversations import Adw, ConversationsPage, GroupMembersDialog, Gtk
    from tests.test_gtk_phone_card import _rows, _settle

    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    client = FakeClient()
    toasts = []
    page = ConversationsPage(client, toasts.append)
    window = Adw.Window(content=page, default_width=900, default_height=700)
    window.present()
    _settle()
    try:
        client.take("threads")[1]([Thread.from_dict(GROUP)])
        page._thread_list.select_row(page._thread_list.get_row_at_index(0))
        _settle()
        # The reply box stays open; Send asks for the members.
        assert page._entry.get_sensitive() and page._send_btn.get_sensitive()
        page._entry.set_text("on my way")

        def send():
            page._on_send(None)
            _settle()
            dialog = window.get_visible_dialog()
            assert isinstance(dialog, GroupMembersDialog)
            return dialog

        dialog = send()
        assert [row.get_title() for row in _rows(dialog.members)] == ["Wife"]
        assert dialog.hint.get_label() == "Still needed: Son"
        assert not dialog.save_button.get_sensitive()

        if scenario == "cancel":
            dialog.close()
            _settle()
            assert window.get_visible_dialog() is None
            assert page._entry.get_text() == "on my way"
            assert not [name for name in client.names() if name[0] in ("participants", "send")]
            # Every attempt asks again.
            send().close()
            _settle()
            return

        dialog.entry.set_text("Son")
        _settle()
        client.take(("contacts", "Son"))[1]([
            ("Son", "+15552222222"), ("Son", "son@icloud.com"), ("Wife", "+15551111111"),
        ])
        suggestions = _rows(dialog.suggestions)
        # One choice per address, minus the ones already in the list.
        assert [(row.get_title(), row.get_subtitle()) for row in suggestions] == [
            ("Son", "+15552222222"), ("Son", "son@icloud.com"),
        ]
        dialog.suggestions.emit("row-activated", suggestions[0])
        assert [row.get_title() for row in _rows(dialog.members)] == ["Wife", "Son"]
        assert not dialog.hint.get_visible()
        assert dialog.save_button.get_sensitive()

        # A raw address is accepted too; something else is not.
        dialog.entry.set_text("not an address")
        dialog.entry.emit("activate")
        assert dialog.problem.get_visible()
        assert len(_rows(dialog.members)) == 2

        dialog.save_button.emit("clicked")
        _settle()
        saved = client.take(
            ("participants", GROUP["key"], ("+15551111111", "+15552222222")),
        )
        assert not [name for name in client.names() if name[0] == "send"]
        saved[1](Thread.from_dict({
            **GROUP, "recipients": ["+15551111111", "+15552222222"], "missing_members": [],
            "reply_ready": True, "participants_required": False,
        }))
        _settle()
        # The draft continues through the normal path: confirm, then send.
        confirm = window.get_visible_dialog()
        assert isinstance(confirm, Adw.AlertDialog)
        confirm.emit("response", "send")
        client.take(("send", GROUP["key"], "on my way", True))
        assert toasts == []
    finally:
        window.close()
        _settle()


if __name__ == "__main__":
    _exercise(sys.argv[1])
