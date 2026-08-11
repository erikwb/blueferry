"""Terminal-client state and headless Textual presentation tests."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from textual.widgets import Input, ListView, TextArea

from blueferry import tui as tui_module
from blueferry.client import BackendError
from blueferry.models import BackendStatus, Thread, ThreadMessage
from blueferry.tui import BlueFerryApp, ConversationItem, MessageRow, TuiState, _initials, _one_line


def _thread(
    key: str,
    name: str,
    body: str,
    *,
    group: bool = False,
    reply_ready: bool = True,
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
            ),
        ),
        last_ts="2026-08-10T10:00:00-04:00",
    )


class _Backend:
    def __init__(self) -> None:
        self.loaded = [
            _thread("one", "Alice", "hello"),
            _thread("group", "Friends", "plans", group=True),
        ]
        self.sent = []

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
    assert state.confirmed_groups == {"group"}


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

            assert app.query_one("#connection-summary").render().plain == "iPhone connected"
            assert app.query_one("#conversation-title").render().plain == "Alice"
            assert len(app.query(MessageRow)) == 1

            app.action_next_thread()
            await pilot.pause(0.1)
            assert state.selected_key == "group"
            assert app.query_one("#conversation-title").render().plain == "Friends  ·  Group"

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
