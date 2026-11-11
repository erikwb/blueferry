"""GTK: opening a message with a draft puts the draft in the composer."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize("scenario", ["after-load", "already-loaded"])
def test_gtk_open_message_draft(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


JORDAN = {
    "key": "address:phone:15551111111", "name": "Jordan", "recipients": ["+15551111111"],
    "reply_ready": True, "last_ts": "2026-11-10T18:00:00Z",
    "messages": [{
        "handle": "1", "body": "you coming?", "timestamp": "2026-11-10T18:00:00Z",
        "outgoing": False, "read": True, "address": "+15551111111",
    }],
}


def _exercise(scenario):
    from blueferry.models import Thread
    from blueferry.ui.conversations import Adw, ConversationsPage, Gtk
    from tests.test_gtk_group_members import FakeClient
    from tests.test_gtk_phone_card import _settle

    Adw.init()
    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    client = FakeClient()
    page = ConversationsPage(client, lambda _message: None)
    window = Adw.Window(content=page, default_width=900, default_height=700)
    window.present()
    _settle()
    try:
        if scenario == "already-loaded":
            client.take("threads")[1]([Thread.from_dict(JORDAN)])
            _settle()
            page.open_message("1", "on my way")
        else:
            page.open_message("1", "on my way")
            _settle()
            while "threads" in client.names():
                client.take("threads")[1]([Thread.from_dict(JORDAN)])
                _settle()
        _settle()
        assert page._state.selected_key == JORDAN["key"]
        assert page._entry.get_text() == "on my way"
        assert not [name for name in client.names() if name[0] == "send"]
    finally:
        window.close()
        _settle()


if __name__ == "__main__":
    _exercise(sys.argv[1])
