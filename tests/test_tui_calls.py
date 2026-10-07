"""Optional calls panel in the Textual client, driven by a fake backend."""
from __future__ import annotations

import asyncio
import time

from textual.widgets import Button, Input, Static

from blueferry.client import BackendError
from blueferry.models import BackendStatus, CallsSnapshot
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_calls import (
    CallsScreen,
    DialConfirmScreen,
    describe,
    hangup_target,
    ringing_call,
)


def _snapshot(*calls, state="ready"):
    return CallsSnapshot.from_dict({"state": state, "calls": [
        {"call_id": call_id, "state": call_state, "number": "+41791234567",
         "contact_name": name}
        for call_id, call_state, name in calls
    ]})


def test_panel_text_and_default_targets() -> None:
    ringing = _snapshot(("voicecall01", "incoming", "Alice\x1b[2J"))
    busy = _snapshot(("voicecall01", "active", ""), ("voicecall02", "held", ""))

    state, text = describe(ringing)
    assert state == "Ready."
    assert "INCOMING" in text and "+41791234567" in text and "\x1b" not in text
    assert ringing_call(ringing) == "voicecall01"
    assert hangup_target(ringing).call_id == "voicecall01"
    assert ringing_call(busy) is None
    assert describe(_snapshot(state="unavailable"))[1] == "No active calls"


def test_hang_up_prefers_the_active_call_and_never_hangs_up_all() -> None:
    held_first = _snapshot(("voicecall02", "held", ""), ("voicecall01", "active", ""))
    waiting = _snapshot(("voicecall01", "active", ""), ("voicecall02", "waiting", "Bob"))
    held_only = _snapshot(("voicecall02", "held", ""), ("voicecall03", "held", ""))

    assert hangup_target(held_first).call_id == "voicecall01"
    assert hangup_target(waiting).call_id == "voicecall01"
    assert hangup_target(held_only).call_id == "voicecall02"
    assert hangup_target(_snapshot()) is None


class _Backend:
    def __init__(self, *, enabled=True) -> None:
        self.enabled = enabled
        self.requests: list[tuple] = []
        self.snapshot = _snapshot(("voicecall01", "incoming", "Alice"))

    def status(self) -> BackendStatus:
        return BackendStatus(daemon=True, map=True, calls_enabled=self.enabled)

    def threads(self, limit: int = 1000):
        return []

    def calls(self) -> CallsSnapshot:
        return self.snapshot

    def dial(self, number: str) -> str:
        if number == "bad":
            raise BackendError("phone number is invalid")
        self.requests.append(("dial", number))
        return "voicecall02"

    def answer_call(self, call_id: str) -> None:
        self.requests.append(("answer", call_id))

    def hangup_call(self, call_id: str) -> None:
        self.requests.append(("hangup", call_id))

    def hangup_all_calls(self) -> None:
        self.requests.append(("hangup_all",))


def _run(coroutine) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coroutine)
    finally:
        loop.close()


async def _until(pilot, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await pilot.pause(0.05)
    assert predicate()


def _confirm_shown(app) -> bool:
    screen = app.screen
    return isinstance(screen, DialConfirmScreen) and bool(screen.query("#calls-confirm-call"))


def test_calls_panel_answers_dials_and_hangs_up() -> None:
    async def scenario() -> None:
        backend = _Backend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.calls_enabled)
            app.set_focus(None)
            await pilot.press("c")
            await _until(pilot, lambda: isinstance(app.screen, CallsScreen))
            screen = app.screen
            await _until(pilot, lambda: not screen.query_one("#calls-answer", Button).disabled)
            assert "Alice" in str(screen.query_one("#calls-list", Static).render())

            screen.answer()
            await _until(pilot, lambda: ("answer", "voicecall01") in backend.requests)
            screen.query_one("#calls-number", Input).value = "+41 79 000 00 00"
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            assert "+41 79 000 00 00" in str(app.screen.query_one(".dialog-copy", Static).render())
            app.screen.action_cancel()
            await _until(pilot, lambda: app.screen is screen)
            assert not any(request[0] == "dial" for request in backend.requests)
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            app.screen.call_button()
            await _until(pilot, lambda: ("dial", "+41 79 000 00 00") in backend.requests)
            screen.hangup()
            await _until(pilot, lambda: ("hangup", "voicecall01") in backend.requests)

            screen.query_one("#calls-number", Input).value = "bad"
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            app.screen.call_button()
            await pilot.pause(0.2)
            assert ("dial", "bad") not in backend.requests
            screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, CallsScreen))

    _run(scenario())


class _CallsMonitor:
    def __init__(self) -> None:
        self.pending = False

    def pump(self):
        return False, None

    def take_calls_changed(self) -> bool:
        pending, self.pending = self.pending, False
        return pending

    def close(self) -> None:
        pass


def test_calls_changed_announces_a_new_ringing_call_once() -> None:
    async def scenario() -> None:
        backend = _Backend()
        monitor = _CallsMonitor()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: monitor)
        notices = []
        app.notify = lambda message, **_kwargs: notices.append(message)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.calls_enabled)
            monitor.pending = True
            await _until(pilot, lambda: bool(notices))
            monitor.pending = True
            await pilot.pause(0.5)
            assert notices == ["Incoming call · press c"]

    _run(scenario())


def test_calls_key_explains_the_opt_in_when_disabled() -> None:
    async def scenario() -> None:
        backend = _Backend(enabled=False)
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            app.set_focus(None)
            app.action_calls()
            await pilot.pause(0.1)
            assert not isinstance(app.screen, CallsScreen)
            assert backend.requests == []

    _run(scenario())


def test_caller_text_is_never_parsed_as_markup() -> None:
    """A hostile contact name must neither link an action nor hide calls."""

    async def scenario() -> None:
        backend = _Backend()
        backend.snapshot = _snapshot(
            ("voicecall01", "incoming", "[@click=app.quit]x[/]"),
            ("voicecall02", "held", "Bob [/nope]"),
        )
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.calls_enabled)
            app.set_focus(None)
            await pilot.press("c")
            await _until(pilot, lambda: isinstance(app.screen, CallsScreen))
            screen = app.screen
            await _until(pilot, lambda: not screen.query_one("#calls-answer", Button).disabled)
            rendered = screen.query_one("#calls-list", Static).render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "[@click=app.quit]x[/]" in text
            assert "Bob [/nope]" in text
            assert all(
                "@click" not in str(getattr(span.style, "meta", "") or "")
                for span in getattr(rendered, "spans", [])
            )
            assert app.is_running

    _run(scenario())


def test_backend_error_toast_is_not_parsed_as_markup() -> None:
    async def scenario() -> None:
        backend = _Backend()

        def failing_dial(_number: str) -> str:
            raise BackendError("[@click=app.quit]boom[/]")

        backend.dial = failing_dial
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.calls_enabled)
            app.set_focus(None)
            await pilot.press("c")
            await _until(pilot, lambda: isinstance(app.screen, CallsScreen))
            screen = app.screen
            seen = []
            screen.notify = lambda message, **kwargs: seen.append((message, kwargs))
            screen.query_one("#calls-number", Input).value = "123"
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            app.screen.call_button()
            await _until(pilot, lambda: bool(seen))
            assert seen[0][1].get("markup") is False

    _run(scenario())


def test_help_lists_the_calls_key_only_when_calls_are_on() -> None:
    from blueferry.tui import HelpScreen

    for enabled in (False, True):
        async def scenario(enabled=enabled) -> None:
            backend = _Backend(enabled=enabled)
            app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
            async with app.run_test(size=(120, 36)) as pilot:
                await _until(pilot, lambda: app.state.status.daemon)
                app.set_focus(None)
                app.action_help()
                await _until(pilot, lambda: isinstance(app.screen, HelpScreen)
                             and bool(app.screen.query(".dialog-copy")))
                text = str(app.screen.query_one(".dialog-copy", Static).render())
                assert ("Phone calls" in text) is enabled

        _run(scenario())
