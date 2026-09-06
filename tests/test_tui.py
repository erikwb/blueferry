"""Terminal-client state and headless Textual presentation tests."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from typing import Any

from textual.widgets import Input, ListView, Static, TextArea

from blueferry import tui as tui_module
from blueferry.client import BackendError
from blueferry.models import BackendStatus, Thread, ThreadMessage
from blueferry.tui import (
    BlueFerryApp,
    ConversationItem,
    DeleteConversationScreen,
    MessageRow,
    RosterChangedScreen,
    TuiState,
    _initials,
    _one_line,
)


def _thread(
    key: str,
    name: str,
    body: str,
    *,
    group: bool = False,
    reply_ready: bool = True,
    roster_changed: bool = False,
) -> Thread:
    return Thread(
        key=key,
        name=name,
        is_group=group,
        recipients=("+15551111111", "+15552222222") if group else ("+15551111111",),
        reply_ready=reply_ready,
        messages=(
            ThreadMessage(
                handle=f"handle-{key}",
                body=body,
                timestamp="2026-08-10T10:00:00-04:00",
                outgoing=False,
                read=False,
                sender="Beau" if group else "",
            ),
        ),
        last_ts="2026-08-10T10:00:00-04:00",
        group_origin="named" if group else "",
        roster_changed=roster_changed,
        unexpected_sender="Beau" if roster_changed else "",
        roster_warning_id="route-1:beau" if roster_changed else "",
    )


class _Backend:
    def __init__(self) -> None:
        self.loaded = [
            _thread("one", "Alice", "hello"),
            _thread("group", "Friends", "plans", group=True),
        ]
        self.sent = []
        self.deleted = []
        self.marked = []
        self.group_participants = None

    @staticmethod
    def status() -> BackendStatus:
        return BackendStatus(daemon=True, map=True, pbap=True, ancs=True)

    def threads(self, limit: int = 1000) -> list[Thread]:
        assert limit == 200
        return self.loaded

    def send_to_thread(self, key: str, body: str, *, confirm_group: bool = False) -> str:
        self.sent.append((key, body, confirm_group))
        return "/transfer/1"

    def send(self, recipient: str, body: str) -> str:
        self.sent.append((recipient, body))
        return "/transfer/2"

    def mark_thread_read(self, thread_key: str) -> int:
        self.marked.append(thread_key)
        return 0

    def delete_threads(self, thread_keys: list[str]) -> int:
        self.deleted.append(list(thread_keys))
        before = len(self.loaded)
        self.loaded = [
            thread for thread in self.loaded if thread.key not in thread_keys
        ]
        return before - len(self.loaded)

    def set_group_participants(
        self, thread_key: str, recipients: list[str],
    ) -> Thread:
        self.group_participants = (thread_key, list(recipients))
        current = next(thread for thread in self.loaded if thread.key == thread_key)
        updated = replace(
            current,
            recipients=tuple(recipients),
            reply_ready=True,
            roster_changed=False,
            unexpected_sender="",
            roster_warning_id="",
        )
        self.loaded = [
            updated if thread.key == thread_key else thread
            for thread in self.loaded
        ]
        return updated


def test_terminal_text_is_sanitized() -> None:
    assert _one_line("safe\n\x1b[31m") == "safe �[31m"


def test_avatar_initials_fill_an_odd_width_symmetrically() -> None:
    assert _initials("Dr. Sarah") == "D S"
    assert _initials("Blueferry") == "B L"
    assert _initials("") == "?"


def test_refresh_preserves_selection_and_navigation_is_bounded() -> None:
    backend = _Backend()
    state = TuiState(backend)

    state.refresh()
    state.move(1)
    state.move(10)
    assert state.selected_key == "group"

    backend.loaded = [backend.loaded[1], backend.loaded[0]]
    state.refresh()
    assert state.selected_key == "group"

    state.move(-10)
    assert state.selected_key == "group"
    state.move(1)
    assert state.selected_key == "one"


def test_notification_handle_selects_its_conversation() -> None:
    state = TuiState(_Backend())
    state.refresh()

    assert state.select_message("handle-group") is True
    assert state.selected_key == "group"
    assert state.select_message("missing") is False


def test_replies_use_opaque_thread_key_and_explicit_group_confirmation() -> None:
    backend = _Backend()
    state = TuiState(backend)
    state.refresh()
    state.selected_key = "group"

    assert state.send_reply("not yet") is False
    assert state.error == "Group reply requires participant confirmation"
    assert state.send_reply("hello all", confirm_group=True) is True

    assert backend.sent == [("group", "hello all", True)]
    assert state.confirmed_groups == set()
    assert state.send_reply("not without another confirmation") is False


def test_message_sender_metadata_is_sanitized() -> None:
    async def scenario() -> None:
        backend = _Backend()
        unsafe = replace(
            backend.loaded[1].messages[0],
            sender="Beau\x1b[31m\u202e\nforged",
        )
        backend.loaded[1] = replace(backend.loaded[1], messages=(unsafe,))
        state = TuiState(backend)
        state.selected_key = "group"
        app = BlueFerryApp(state, monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 2)
            app.action_next_thread()
            await _wait_for_conversation_title(app, pilot, "Friends  ·  Group")
            meta = await _wait_for_message_meta(
                app, pilot, "Beau�[31m� forged  ·  "
            )
            assert meta.render().plain.startswith("Beau�[31m� forged  ·  ")

    _run_headless(scenario())


def test_read_only_thread_cannot_send() -> None:
    backend = _Backend()
    backend.loaded = [_thread("unsafe", "Unknown group", "hello", reply_ready=False)]
    state = TuiState(backend)
    state.refresh()

    assert state.send_reply("nope") is False
    assert backend.sent == []
    assert state.error == "This conversation is read-only"


def test_new_message_uses_explicit_destination() -> None:
    backend = _Backend()
    state = TuiState(backend)
    state.refresh()

    assert state.send_new("person@example.com", "hello") is True

    assert backend.sent == [("person@example.com", "hello")]


def test_delete_uses_one_opaque_thread_key() -> None:
    backend = _Backend()
    state = TuiState(backend)
    state.refresh()

    assert state.delete_thread("one") is True

    assert backend.deleted == [["one"]]
    assert [thread.key for thread in state.threads] == ["group"]
    assert state.notice == "Conversation deleted locally"


def test_group_participant_update_replaces_local_thread_snapshot() -> None:
    backend = _Backend()
    backend.loaded = [
        _thread(
            "group", "Friends", "new here", group=True,
            reply_ready=False, roster_changed=True,
        )
    ]
    state = TuiState(backend)
    state.refresh()

    assert state.save_group_participants(
        "group", ["+15551111111", "+15553333333"]
    ) is True
    assert backend.group_participants == (
        "group", ["+15551111111", "+15553333333"]
    )
    assert state.selected is not None
    assert state.selected.reply_ready is True
    assert state.notice == "Group participants saved locally"


def test_send_failures_remain_user_visible() -> None:
    class FailingBackend(_Backend):
        def send_to_thread(self, key: str, body: str, *, confirm_group: bool = False) -> str:
            raise BackendError("phone unavailable")

    state = TuiState(FailingBackend())
    state.refresh()

    assert state.send_reply("hello") is False
    assert state.error == "phone unavailable"


def test_noninteractive_invocation_stops_before_backend_io(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_module,
        "ensure_backend_current",
        lambda: (_ for _ in ()).throw(AssertionError("backend was contacted")),
    )
    monkeypatch.setattr(tui_module.sys.stdin, "isatty", lambda: False)

    assert tui_module.main() == 2


async def _wait_for_threads(app: BlueFerryApp, pilot, count: int) -> None:
    for _attempt in range(30):
        if len(app.query(ConversationItem)) == count:
            await pilot.pause(0.1)
            return
        await pilot.pause(0.05)
    assert len(app.query(ConversationItem)) == count


async def _wait_for_conversation_title(
    app: BlueFerryApp, pilot, expected: str,
) -> None:
    await _wait_for_static_text(app, pilot, "#conversation-title", expected)


async def _wait_for_static_text(
    app: BlueFerryApp, pilot, selector: str, expected: str,
) -> Static:
    widget = app.query_one(selector, Static)
    for _attempt in range(30):
        if widget.render().plain == expected:
            return widget
        await pilot.pause(0.05)
    assert widget.render().plain == expected
    return widget


async def _wait_for_static_text_containing(
    app: BlueFerryApp, pilot, selector: str, expected: str,
) -> Static:
    widget = app.query_one(selector, Static)
    for _attempt in range(30):
        if expected in widget.render().plain:
            return widget
        await pilot.pause(0.05)
    assert expected in widget.render().plain
    return widget


async def _wait_for_message_meta(
    app: BlueFerryApp, pilot, expected_prefix: str,
) -> Static:
    for _attempt in range(30):
        for widget in app.query(".message-meta"):
            if (
                isinstance(widget, Static)
                and widget.render().plain.startswith(expected_prefix)
            ):
                return widget
        await pilot.pause(0.05)
    meta = app.query_one(MessageRow).query_one(".message-meta", Static)
    assert meta.render().plain.startswith(expected_prefix)
    return meta


def _run_headless(coroutine: Coroutine[Any, Any, None]) -> None:
    # Python 3.14's asyncio.Runner waits unnecessarily for Textual's already
    # drained async generators. A directly-owned loop is deterministic here;
    # App.run_test() performs its own task cleanup before returning.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coroutine)
    finally:
        loop.close()


def test_textual_app_renders_status_threads_and_messages() -> None:
    async def scenario() -> None:
        state = TuiState(_Backend())
        app = BlueFerryApp(state, monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 2)

            connection = await _wait_for_static_text(
                app, pilot, "#connection-summary", "iPhone connected"
            )
            title = await _wait_for_static_text(
                app, pilot, "#conversation-title", "Alice"
            )
            direct_meta = await _wait_for_message_meta(app, pilot, "Alice  ·  ")
            assert connection.render().plain == "iPhone connected"
            assert title.render().plain == "Alice"
            assert direct_meta.render().plain.startswith("Alice  ·  ")

            app.action_next_thread()
            await _wait_for_conversation_title(app, pilot, "Friends  ·  Group")
            assert state.selected_key == "group"
            assert app.query_one("#conversation-title").render().plain == "Friends  ·  Group"
            meta = await _wait_for_message_meta(app, pilot, "Beau  ·  ")
            assert meta.render().plain.startswith("Beau  ·  ")

    _run_headless(scenario())


def test_delete_key_confirms_and_deletes_current_conversation() -> None:
    async def scenario() -> None:
        backend = _Backend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 2)
            await pilot.press("delete")

            assert isinstance(app.screen, DeleteConversationScreen)
            assert app.screen.thread.key == "one"
            assert backend.deleted == []

            confirmation = app.screen
            await pilot.press("delete")
            assert app.screen is confirmation
            assert backend.deleted == []

            await pilot.click("#delete-confirm")
            for _attempt in range(30):
                if backend.deleted:
                    break
                await pilot.pause(0.05)

            assert backend.deleted == [["one"]]
            await _wait_for_threads(app, pilot, 1)
            assert app.state.selected_key == "group"

    _run_headless(scenario())


def test_textual_warns_about_bluez_restart_when_only_ancs_is_missing(
    monkeypatch,
) -> None:
    class MissingAncsBackend(_Backend):
        @staticmethod
        def status() -> BackendStatus:
            return BackendStatus(daemon=True, map=True, pbap=True, ancs=False)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(MissingAncsBackend()), monitor_factory=lambda: None)

        async with app.run_test(size=(70, 36)) as pilot:
            notice = await _wait_for_static_text_containing(
                app,
                pilot,
                "#notice-bar",
                "sudo systemctl restart bluetooth.service",
            )
            assert notice.has_class("warn")
            for _attempt in range(30):
                if notice.region.height >= 3:
                    break
                await pilot.pause(0.05)
            assert notice.region.height >= 3

    monkeypatch.setattr(tui_module.config, "ANCS_ENABLED", True)
    _run_headless(scenario())


def test_unchanged_poll_does_not_rebuild_the_terminal_view() -> None:
    async def scenario() -> None:
        state = TuiState(_Backend())
        app = BlueFerryApp(state, monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 2)
            await pilot.pause(0.1)
            thread_items = tuple(app.query_one("#thread-list", ListView).children)
            message_items = tuple(
                app.query_one("#message-timeline").children
            )

            await app._apply_snapshot(state.fetch_snapshot())
            await pilot.pause(0.1)

            assert tuple(app.query_one("#thread-list", ListView).children) == thread_items
            assert tuple(app.query_one("#message-timeline").children) == message_items

    _run_headless(scenario())


def test_textual_warns_once_when_saved_group_roster_changes() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.loaded = [
            _thread(
                "group", "Friends", "new here", group=True,
                reply_ready=False, roster_changed=True,
            )
        ]
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 1)
            await pilot.pause(0.1)
            assert isinstance(app.screen, RosterChangedScreen)
            await pilot.press("escape")
            app.action_refresh()
            await pilot.pause(0.2)
            assert not isinstance(app.screen, RosterChangedScreen)

    _run_headless(scenario())


def test_textual_keeps_participant_editor_available_after_dismissal() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.loaded = [
            _thread(
                "group", "Friends", "new here", group=True,
                reply_ready=False, roster_changed=True,
            )
        ]
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 1)
            await pilot.pause(0.1)
            await pilot.press("escape")
            button = app.query_one("#edit-group-participants")
            assert button.display is True

            await pilot.click("#edit-group-participants")
            assert isinstance(app.screen, RosterChangedScreen)

    _run_headless(scenario())


def test_textual_roster_warning_can_save_reviewed_participants() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.loaded = [
            _thread(
                "group", "Friends", "new here", group=True,
                reply_ready=False, roster_changed=True,
            )
        ]
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 1)
            await pilot.pause(0.1)
            editor = app.screen.query_one("#roster-participants", TextArea)
            editor.text = "+15551111111\n+15553333333"
            await pilot.click("#roster-changed-save")
            for _attempt in range(30):
                if backend.group_participants is not None:
                    break
                await pilot.pause(0.05)

            assert backend.group_participants == (
                "group", ["+15551111111", "+15553333333"]
            )
            assert app.state.selected is not None
            assert app.state.selected.reply_ready is True

    _run_headless(scenario())


def test_textual_reopens_participant_editor_after_save_failure() -> None:
    class FailingBackend(_Backend):
        def set_group_participants(
            self, thread_key: str, recipients: list[str],
        ) -> Thread:
            raise BackendError("participant list rejected")

    async def scenario() -> None:
        backend = FailingBackend()
        backend.loaded = [
            _thread(
                "group", "Friends", "new here", group=True,
                reply_ready=False, roster_changed=True,
            )
        ]
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 1)
            await pilot.pause(0.1)
            await pilot.click("#roster-changed-save")
            for _attempt in range(30):
                if (
                    isinstance(app.screen, RosterChangedScreen)
                    and app.state.error == "participant list rejected"
                ):
                    break
                await pilot.pause(0.05)

            assert isinstance(app.screen, RosterChangedScreen)
            assert app.state.error == "participant list rejected"

    _run_headless(scenario())


def test_textual_search_and_narrow_conversation_navigation() -> None:
    async def scenario() -> None:
        app = BlueFerryApp(TuiState(_Backend()), monitor_factory=lambda: None)

        async with app.run_test(size=(70, 30)) as pilot:
            await _wait_for_threads(app, pilot, 2)
            workspace = app.query_one("#workspace")
            assert workspace.has_class("narrow")
            assert not workspace.has_class("chat-open")

            search = app.query_one("#thread-search", Input)
            search.value = "friends"
            await _wait_for_threads(app, pilot, 1)
            assert len(app.query_one("#thread-list", ListView).children) == 1

            app.action_open_thread()
            assert workspace.has_class("chat-open")
            app.action_return_to_list()
            assert not workspace.has_class("chat-open")

    _run_headless(scenario())


def test_textual_composer_sends_without_blocking_ui() -> None:
    async def scenario() -> None:
        backend = _Backend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)

        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_threads(app, pilot, 2)
            composer = app.query_one("#composer", TextArea)
            composer.focus()
            compact_height = composer.size.height
            composer.text = "\n".join(f"Line {index}" for index in range(20))
            await pilot.pause()
            assert compact_height < composer.size.height <= 8
            assert composer.max_scroll_y > 0

            composer.text = "Line one"
            await pilot.press("shift+enter")
            assert "\n" in composer.text
            assert backend.sent == []

            composer.text = "A proper terminal reply"
            await pilot.press("enter")
            for _attempt in range(30):
                if backend.sent:
                    break
                await pilot.pause(0.05)

            assert backend.sent == [("one", "A proper terminal reply", False)]

    _run_headless(scenario())
