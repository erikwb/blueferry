"""Regression tests for conversation-list refresh behavior."""
from __future__ import annotations

from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")

from blueferry.conversation_state import (  # noqa: E402
    ConversationSnapshot,
    ConversationState,
)
from blueferry.models import BackendStatus, Thread, ThreadMessage  # noqa: E402
from blueferry.ui import conversations  # noqa: E402


class _FakeWidget:
    def __init__(self, **_kwargs):
        self.children = []

    def append(self, child):
        self.children.append(child)


class _FakeGesture:
    def __init__(self, **_kwargs):
        pass

    def connect(self, *_args):
        pass


class _FakeRow:
    def __init__(self):
        self.child = None
        self.controllers = []

    def set_child(self, child):
        self.child = child

    def add_controller(self, controller):
        self.controllers.append(controller)


class _FakeGtk:
    class Orientation:
        VERTICAL = 1

    ListBoxRow = _FakeRow
    Box = _FakeWidget
    Label = _FakeWidget
    GestureClick = _FakeGesture


class _FakeGdk:
    BUTTON_SECONDARY = 3


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


def _thread(**changes) -> Thread:
    values = {
        "key": "Alice",
        "name": "Alice",
        "is_group": False,
        "recipients": (),
        "reply_ready": True,
        "messages": (
            ThreadMessage(
                handle="1",
                body="hello",
                timestamp="2026-08-08T10:00:00-04:00",
                outgoing=False,
                read=True,
            ),
        ),
        "last_ts": "2026-08-08T10:00:00-04:00",
    }
    values.update(changes)
    return Thread(**values)


def test_sidebar_rebuild_does_not_fire_selection_callback(monkeypatch):
    """A live event redraw must not redraw the current thread a second time."""
    monkeypatch.setattr(conversations, "Gtk", _FakeGtk)
    monkeypatch.setattr(conversations, "Gdk", _FakeGdk)
    thread_list = _FakeThreadList()
    state = ConversationState(select_first=False)
    state.apply_snapshot(ConversationSnapshot(None, (_thread(),)))
    state.selected_key = "Alice"
    page = SimpleNamespace(
        _thread_list=thread_list,
        _thread_selected_handler=42,
        _show_thread_context_menu=lambda *_args: None,
        _state=state,
    )

    conversations.ConversationsPage._rebuild_thread_list(page)

    assert len(thread_list.rows) == 1
    assert len(thread_list.rows[0].controllers) == 1
    assert thread_list.selection_callbacks == 0
    assert thread_list.blocked is False


def test_map_refusal_reveals_prominent_message_banner() -> None:
    class Banner:
        revealed = False

        def set_revealed(self, value):
            self.revealed = value

    banner = Banner()
    page = SimpleNamespace(
        _map_refused_banner=banner,
        _state=ConversationState(select_first=False),
    )

    result = conversations.ConversationsPage._apply_status(
        page,
        BackendStatus(
            connectivity_state="map-connection-refused",
            connectivity_detail="Connection refused (111)",
        ),
    )

    assert result is False
    assert banner.revealed is True


def test_gtk_message_composer_sends_on_enter_and_keeps_shift_enter() -> None:
    submitted = []
    composer = SimpleNamespace(
        _on_submit=lambda widget: submitted.append(widget),
    )

    handled = conversations.MessageComposer._key_pressed(
        composer,
        None,
        conversations.Gdk.KEY_Return,
        0,
        conversations.Gdk.ModifierType(0),
    )
    shift_handled = conversations.MessageComposer._key_pressed(
        composer,
        None,
        conversations.Gdk.KEY_Return,
        0,
        conversations.Gdk.ModifierType.SHIFT_MASK,
    )

    assert handled is True
    assert shift_handled is False
    assert submitted == [composer]


def test_gtk_message_composer_placeholder_tracks_empty_buffer() -> None:
    class Placeholder:
        visible = None

        def set_visible(self, value) -> None:
            self.visible = value

    placeholder = Placeholder()
    resize_requests = []
    composer = SimpleNamespace(
        _placeholder=placeholder,
        queue_resize=lambda: resize_requests.append(True),
    )

    conversations.MessageComposer._content_changed(
        composer, SimpleNamespace(get_char_count=lambda: 0)
    )
    assert placeholder.visible is True

    conversations.MessageComposer._content_changed(
        composer, SimpleNamespace(get_char_count=lambda: 12)
    )
    assert placeholder.visible is False
    assert resize_requests == [True, True]


def test_participant_editor_keeps_unique_nonempty_lines() -> None:
    assert conversations._participant_lines(
        " +15551111111 \n\nbeau@example.com\n+15551111111\n"
    ) == ["+15551111111", "beau@example.com"]


def test_roster_banner_keeps_unexpected_sender_after_later_known_sender() -> None:
    title = conversations._group_roster_banner_title(_thread(
        name="Crew",
        roster_changed=True,
        unexpected_sender="Casey",
        prompt_sender="Beau",
    ))

    assert title.startswith("Casey is not")
    assert "Beau" not in title


def test_roster_warning_fallback_id_is_stable_for_partial_payload() -> None:
    thread = _thread(
        key="group:named:crew",
        unexpected_sender="Casey",
        roster_warning_id="",
    )

    assert ConversationState.roster_warning_id(thread) == (
        "group:named:crew:Casey"
    )
