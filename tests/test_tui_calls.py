"""Optional calls panel in the Textual client, driven by a fake backend."""
from __future__ import annotations

import asyncio
import threading
import time

from textual.widgets import Button, Input, Static

from blueferry.client import BackendError
from blueferry.models import BackendStatus, CallsSnapshot
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_calls import (
    CallsScreen,
    DialConfirmScreen,
    calls_text,
    hangup_target,
    hold_target,
    ringing_call,
    state_text,
    tones,
)


def _snapshot(*calls, state="ready"):
    return CallsSnapshot.from_dict({"state": state, "calls": [
        {"call_id": call_id, "state": call_state, "number": "+41791234567",
         "contact_name": name, "first_seen": "2026-10-27T10:00:00+00:00"}
        for call_id, call_state, name in calls
    ]})


_NOW = 1_793_095_265.0  # 2026-10-27T10:01:05+00:00


def test_panel_text_uses_the_shared_call_card_wording() -> None:
    ringing = _snapshot(("voicecall01", "incoming", "Alice\x1b[2J"))
    busy = _snapshot(("voicecall01", "active", ""), ("voicecall02", "held", ""))

    text = calls_text(ringing, _NOW).plain
    assert "Incoming call…" in text and "+41791234567" in text and "\x1b" not in text
    busy_text = calls_text(busy, _NOW).plain
    assert "1:05" in busy_text and "On hold" in busy_text
    assert ringing_call(ringing) == "voicecall01"
    assert hangup_target(ringing).call_id == "voicecall01"
    assert ringing_call(busy) is None
    assert hold_target(busy).call_id == "voicecall01"
    assert calls_text(_snapshot(state="unavailable"), _NOW).plain == "No active calls"


def test_state_text_follows_calls_hint_and_keeps_special_states() -> None:
    on = {"calls_enabled": True}
    assert state_text(on, _snapshot()) == ""
    assert "oFono" in state_text(on, _snapshot(state="unavailable"))
    assert "Settings" in state_text({"calls_enabled": False}, _snapshot(state="disabled"))
    assert "-P hfp" in state_text(on, _snapshot(state="bluez_conflict"))


def test_tone_field_keeps_only_keypad_characters() -> None:
    assert tones("1a2*#b ") == "12*#"
    assert len(tones("1" * 50)) == 32


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
        self.contacts: dict[str, list[tuple[str, str]]] = {}

    def status(self) -> BackendStatus:
        return BackendStatus(
            daemon=True, map=True, calls_enabled=self.enabled,
            calls_state="ready" if self.enabled else "disabled",
        )

    def threads(self, limit: int = 1000):
        return []

    def calls(self) -> CallsSnapshot:
        return self.snapshot

    def dial(self, number: str) -> str:
        if number == "999":
            raise BackendError("phone number is invalid")
        self.requests.append(("dial", number))
        return "voicecall02"

    def answer_call(self, call_id: str) -> None:
        self.requests.append(("answer", call_id))

    def hangup_call(self, call_id: str) -> None:
        self.requests.append(("hangup", call_id))

    def hangup_all_calls(self) -> None:
        self.requests.append(("hangup_all",))

    def swap_calls(self) -> None:
        self.requests.append(("swap",))

    def send_call_tones(self, call_id: str, tones: str) -> None:
        self.requests.append(("tones", call_id, tones))

    def find_contacts(self, query: str) -> list[tuple[str, str]]:
        self.requests.append(("find", query))
        return self.contacts.get(query, [])


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
            assert "+41790000000" in str(app.screen.query_one(".dialog-copy", Static).render())
            app.screen.action_cancel()
            await _until(pilot, lambda: app.screen is screen)
            assert not any(request[0] == "dial" for request in backend.requests)
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            app.screen.call_button()
            await _until(pilot, lambda: ("dial", "+41790000000") in backend.requests)
            screen.hangup()
            await _until(pilot, lambda: ("hangup", "voicecall01") in backend.requests)

            screen.query_one("#calls-number", Input).value = "999"
            screen.dial()
            await _until(pilot, lambda: _confirm_shown(app))
            app.screen.call_button()
            await pilot.pause(0.2)
            assert ("dial", "999") not in backend.requests
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


async def _open_calls(app, pilot) -> CallsScreen:
    await _until(pilot, lambda: app.state.status.calls_enabled)
    app.set_focus(None)
    await pilot.press("c")
    await _until(pilot, lambda: isinstance(app.screen, CallsScreen))
    screen = app.screen
    await _until(pilot, lambda: screen.loaded)
    return screen


def test_hold_resume_and_keypad_tones_for_the_active_call() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.snapshot = _snapshot(("voicecall01", "active", "Alice"))
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_calls(app, pilot)
            hold = screen.query_one("#calls-hold", Button)
            assert str(hold.label) == "Hold" and not hold.disabled
            assert str(screen.query_one("#calls-hangup", Button).label) == "Hang Up"
            assert screen.query_one("#calls-tones-row").display
            screen.hold()
            await _until(pilot, lambda: ("swap",) in backend.requests)

            field = screen.query_one("#calls-tones", Input)
            field.focus()
            await pilot.press("1", "x", "2", "#", "enter")
            await _until(pilot, lambda: ("tones", "voicecall01", "12#") in backend.requests)
            await _until(pilot, lambda: field.value == "")

            backend.snapshot = _snapshot(("voicecall01", "held", "Alice"))
            screen.refresh_calls()
            await _until(pilot, lambda: str(hold.label) == "Resume")
            assert not screen.query_one("#calls-tones-row").display
            assert backend.requests.count(("hangup_all",)) == 0

    _run(scenario())


def test_buttons_stay_disabled_while_an_action_runs_even_if_status_changes() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.snapshot = _snapshot(("voicecall01", "active", "Alice"))
        release = threading.Event()
        started = threading.Event()

        def slow_hangup(call_id: str) -> None:
            started.set()
            release.wait(5)
            backend.requests.append(("hangup", call_id))

        backend.hangup_call = slow_hangup
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_calls(app, pilot)
            screen.hangup()
            await _until(pilot, started.is_set)
            screen.status_changed(app.state.status.to_dict())
            screen.refresh_calls()
            await pilot.pause(0.2)
            for selector in ("#calls-hangup", "#calls-hold", "#calls-tones-send", "#calls-dial"):
                assert screen.query_one(selector, Button).disabled, selector
            screen.hangup()  # ignored while busy
            release.set()
            await _until(pilot, lambda: not screen.busy)
            assert backend.requests.count(("hangup", "voicecall01")) == 1
            assert not screen.query_one("#calls-hangup", Button).disabled

    _run(scenario())


def test_late_answers_after_closing_the_panel_are_dropped() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.snapshot = _snapshot(("voicecall01", "active", "Alice"))
        release = threading.Event()
        blocked = threading.Event()
        original_calls = backend.calls

        def slow_calls():
            if blocked.is_set():
                release.wait(5)
            return original_calls()

        def slow_swap() -> None:
            release.wait(5)
            raise BackendError("too late")

        backend.calls = slow_calls
        backend.swap_calls = slow_swap
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        notices = []
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_calls(app, pilot)
            screen.notify = lambda message, **_kw: notices.append(message)
            blocked.set()
            screen.refresh_calls()
            screen.hold()
            screen.action_close()
            await _until(pilot, lambda: not screen.is_attached)
            release.set()
            await pilot.pause(0.3)
            assert app.is_running
            assert notices == []

    _run(scenario())


def test_dial_suggestions_offer_phone_numbers_and_ignore_stale_results() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.snapshot = _snapshot()
        backend.contacts = {
            "An": [("[b]Ana[/b]", "+41 79 111 22 33"), ("Ana", "ana@icloud.com")],
        }
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open_calls(app, pilot)
            field = screen.query_one("#calls-number", Input)
            field.focus()
            await pilot.press("A", "n")
            options = screen.query_one("#calls-suggestions")
            await _until(pilot, lambda: options.option_count == 1)
            assert options.display
            assert "[b]Ana[/b]" in options.get_option_at_index(0).prompt.plain
            options.highlighted = 0
            options.action_select()
            await _until(pilot, lambda: field.value == "+41791112233")
            await pilot.pause(0.4)
            assert not options.display
            assert backend.requests.count(("find", "An")) == 1

            # A reply for a query the user has typed past is not shown.
            request = screen._contacts.begin_contact_search("Old")
            screen._contacts.begin_contact_search("Newer")
            assert not screen._contacts.apply_contact_results(request, [("X", "1")])

    _run(scenario())


def test_calls_disabled_hint_names_the_setting() -> None:
    async def scenario() -> None:
        backend = _Backend(enabled=False)
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        seen = []
        app.notify = lambda message, **kwargs: seen.append((message, kwargs))
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            app.set_focus(None)
            app.action_calls()
            await pilot.pause(0.1)
            assert seen and "Calls & Audio" in seen[-1][0]
            assert seen[-1][1].get("markup") is False

    _run(scenario())
