"""Regression tests for conversation-list refresh behavior."""
from __future__ import annotations

from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")

from blueferry.models import BackendStatus  # noqa: E402
from blueferry.ui import conversations  # noqa: E402


class _FakeWidget:
    def __init__(self, **_kwargs):
        self.children = []

    def append(self, child):
        self.children.append(child)


class _FakeRow:
    def __init__(self):
        self.child = None

    def set_child(self, child):
        self.child = child


class _FakeGtk:
    class Orientation:
        VERTICAL = 1

    ListBoxRow = _FakeRow
    Box = _FakeWidget
    Label = _FakeWidget


class _FakeThreadList:
    def __init__(self):
        self.blocked = False
        self.rows = []
        self.selection_callbacks = 0

    def handler_block(self, _handler):
        self.blocked = True

    def handler_unblock(self, _handler):
        self.blocked = False

    def remove_all(self):
        self.rows.clear()

    def append(self, row):
        self.rows.append(row)

    def select_row(self, _row):
        if not self.blocked:
            self.selection_callbacks += 1


def test_sidebar_rebuild_does_not_fire_selection_callback(monkeypatch):
    """A live event redraw must not redraw the current thread a second time."""
    monkeypatch.setattr(conversations, "Gtk", _FakeGtk)
    thread_list = _FakeThreadList()
    page = SimpleNamespace(
        _thread_list=thread_list,
        _thread_selected_handler=42,
        _current="Alice",
        _threads={
            "Alice": {
                "key": "Alice",
                "name": "Alice",
                "messages": [{"body": "hello"}],
                "last_ts": "2026-08-08T10:00:00-04:00",
            },
        },
    )

    conversations.ConversationsPage._rebuild_thread_list(page)

    assert len(thread_list.rows) == 1
    assert thread_list.selection_callbacks == 0
    assert thread_list.blocked is False


def test_map_refusal_reveals_prominent_message_banner() -> None:
    class Banner:
        revealed = False

        def set_revealed(self, value):
            self.revealed = value

    banner = Banner()
    page = SimpleNamespace(_map_refused_banner=banner)

    result = conversations.ConversationsPage._apply_status(
        page,
        BackendStatus(
            connectivity_state="map-connection-refused",
            connectivity_detail="Connection refused (111)",
        ),
    )

    assert result is False
    assert banner.revealed is True
