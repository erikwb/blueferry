"""Regression tests for conversation-list refresh behavior."""
from __future__ import annotations

from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")

from blueferry.conversation_state import (  # noqa: E402
    ConversationSnapshot,
    ConversationState,
)
from blueferry.models import BackendStatus, Thread, ThreadMessage  # noqa: E402
from blueferry.ui import conversations  # noqa: E402


class _FakeWidget:
    def __init__(self, **kwargs):
        self.children = []
        self.css_classes = kwargs.get("css_classes", [])

    def append(self, child):
        self.children.append(child)

    def connect(self, *_args):
        pass


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
        HORIZONTAL = 2

    class Align:
        CENTER = 1

    ListBoxRow = _FakeRow
    Box = _FakeWidget
    Label = _FakeWidget
    Button = _FakeWidget
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


def _name_label(row):
    return row.child.children[0].children[0]


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
        _toggle_star=lambda *_args: None,
        _state=state,
    )

    conversations.ConversationsPage._rebuild_thread_list(page)

    assert len(thread_list.rows) == 1
    assert len(thread_list.rows[0].controllers) == 1
    assert thread_list.selection_callbacks == 0
    assert thread_list.blocked is False
    assert _name_label(thread_list.rows[0]).css_classes == []


def test_unread_thread_name_uses_heading_style(monkeypatch):
    monkeypatch.setattr(conversations, "Gtk", _FakeGtk)
    monkeypatch.setattr(conversations, "Gdk", _FakeGdk)
    thread_list = _FakeThreadList()
    unread = _thread(
        key="Bob",
        name="Bob",
        messages=(
            ThreadMessage(
                handle="2",
                body="hey",
                timestamp="2026-08-08T11:00:00-04:00",
                outgoing=False,
                read=False,
            ),
        ),
    )
    state = ConversationState(select_first=False)
    state.apply_snapshot(ConversationSnapshot(None, (unread,)))
    page = SimpleNamespace(
        _thread_list=thread_list,
        _thread_selected_handler=42,
        _show_thread_context_menu=lambda *_args: None,
        _toggle_star=lambda *_args: None,
        _state=state,
    )

    conversations.ConversationsPage._rebuild_thread_list(page)

    assert _name_label(thread_list.rows[0]).css_classes == ["heading"]


def test_star_button_toggles_backend_star_state():
    starred = []
    unread = _thread(key="Bob", name="Bob", starred=True)
    state = ConversationState(select_first=False)
    state.apply_snapshot(ConversationSnapshot(None, (unread,)))
    page = SimpleNamespace(
        _state=state,
        _client=SimpleNamespace(
            set_thread_starred_async=lambda key, value: starred.append((key, value))
        ),
    )

    conversations.ConversationsPage._toggle_star(page, None, "Bob", False)
    assert starred == [("Bob", False)]


@pytest.mark.parametrize("mapped,active", [(True, True), (False, True), (True, False)])
def test_selected_unread_thread_is_marked_read(mapped, active):
    marked = []
    unread = _thread(
        key="Bob",
        name="Bob",
        messages=(
            ThreadMessage(
                handle="2",
                body="hey",
                timestamp="2026-08-08T11:00:00-04:00",
                outgoing=False,
                read=False,
            ),
        ),
    )
    state = ConversationState(select_first=False)
    state.apply_snapshot(ConversationSnapshot(None, (unread, _thread())))
    state.selected_key = "Bob"
    page = SimpleNamespace(
        get_root=lambda: SimpleNamespace(is_active=lambda: active),
        get_mapped=lambda: mapped,
        _state=state,
        _client=SimpleNamespace(
            mark_thread_read_async=lambda key: marked.append(key)
        ),
    )

    conversations.ConversationsPage._mark_selected_read(page)
    assert marked == (["Bob"] if mapped and active else [])

    state.selected_key = "Alice"
    conversations.ConversationsPage._mark_selected_read(page)
    assert marked == (["Bob"] if mapped and active else [])


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


@pytest.mark.parametrize(
    ("position", "changed", "expected"),
    [(200, True, [200]), (780, True, [None]), (200, False, [])],
)
def test_refresh_preserves_reading_position_and_only_follows_from_bottom(position, changed, expected):
    from unittest.mock import Mock

    before = _thread()
    state = ConversationState()
    state.apply_snapshot(ConversationSnapshot(None, (before,)))
    adjustment = Mock()
    adjustment.get_value.return_value = position
    adjustment.get_upper.return_value = 1000
    adjustment.get_page_size.return_value = 200
    page = Mock(_state=state)
    page._msg_scroll.get_vadjustment.return_value = adjustment
    after = _thread(messages=(*before.messages, before.messages[0])) if changed else before

    conversations.ConversationsPage._apply_threads(page, [after])

    assert [call.args[0] for call in page._scroll_to.call_args_list] == expected
    assert page._msg_list.remove_all.call_count == int(changed)


def test_delayed_scroll_does_not_move_a_different_conversation(monkeypatch):
    from unittest.mock import Mock

    callbacks = []
    monkeypatch.setattr(conversations.GLib, "idle_add", lambda callback: callbacks.append(callback))
    page = Mock(_state=SimpleNamespace(selected_key="Alice"))
    conversations.ConversationsPage._scroll_to(page, 200)
    page._state.selected_key = "Bob"
    assert callbacks[0]() is False
    page._msg_scroll.get_vadjustment.assert_not_called()
