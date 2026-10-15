"""Notification click-rule editor in the Textual client, driven by a fake backend."""
from __future__ import annotations

import asyncio
import time

from textual.widgets import Button, Input, OptionList, Static

from blueferry.client import BackendError
from blueferry.models import BackendStatus
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_notification_rules import (
    POLICY_NOTE,
    NotificationRulesScreen,
    rule_line,
    validate_rule,
)


class _Backend:
    def __init__(self, *, supported=True, policy="all") -> None:
        self.supported = supported
        self.policy = policy
        self.requests: list[tuple] = []
        self.rules = [
            {"bundle_id": "com.slack", "kind": "desktop", "target": "slack.desktop"},
        ]

    def status(self) -> BackendStatus:
        extra = {"notification_open_map_revision": 3} if self.supported else {}
        return BackendStatus.from_dict({
            "daemon": True, "map": True, "notification_policy": self.policy, **extra,
        })

    def threads(self, limit: int = 1000):
        return []

    def notification_open_map(self):
        self.requests.append(("list",))
        return list(self.rules)

    def set_notification_open_target(self, bundle_id, target):
        if target == "https://refused.example/":
            raise BackendError("the backend refused this rule")
        self.requests.append(("set", bundle_id, target))
        kind = "desktop" if target.endswith(".desktop") else "url"
        self.rules = [rule for rule in self.rules if rule["bundle_id"] != bundle_id]
        self.rules.append({"bundle_id": bundle_id, "kind": kind, "target": target})
        return list(self.rules)

    def remove_notification_open_target(self, bundle_id):
        self.requests.append(("remove", bundle_id))
        before = len(self.rules)
        self.rules = [rule for rule in self.rules if rule["bundle_id"] != bundle_id]
        return len(self.rules) != before


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


def test_rule_rows_and_local_validation() -> None:
    row = rule_line({"bundle_id": "com.a", "kind": "url", "target": "https://x/\x1b[2J"}, 8)
    assert row.startswith("com.a     url")
    assert "\x1b" not in row
    assert validate_rule("", "https://example.com/") is not None
    assert validate_rule("com.slack", "javascript:alert(1)") is not None
    assert validate_rule("com.slack", "../evil.desktop") is not None
    assert validate_rule("com.slack", "https://example.com/") is None
    assert validate_rule("com.slack", "slack.desktop") is None


def test_rules_panel_lists_adds_and_removes_rules() -> None:
    async def scenario() -> None:
        backend = _Backend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            app.set_focus(None)
            await pilot.press("l")
            await _until(pilot, lambda: isinstance(app.screen, NotificationRulesScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.rules == backend.rules)
            assert screen.query("#rules-bundle")
            assert not screen.query("#rules-policy-note")
            options = screen.query_one("#rules-list", OptionList)
            assert options.option_count == 1

            # An invalid draft never reaches the backend.
            screen.query_one("#rules-bundle", Input).value = "com.example.Mail"
            screen.query_one("#rules-target", Input).value = "file:///etc/passwd"
            screen.save_rule()
            await pilot.pause(0.1)
            assert not any(request[0] == "set" for request in backend.requests)

            # A backend refusal keeps the form for correction.
            screen.query_one("#rules-target", Input).value = "https://refused.example/"
            screen.save_rule()
            await pilot.pause(0.2)
            assert screen.query_one("#rules-bundle", Input).value == "com.example.Mail"

            screen.query_one("#rules-target", Input).value = "https://mail.example.com/"
            screen.save_rule()
            await _until(pilot, lambda: options.option_count == 2)
            assert (
                "set", "com.example.Mail", "https://mail.example.com/"
            ) in backend.requests
            assert screen.query_one("#rules-bundle", Input).value == ""

            # Enter on a rule copies it into the form; Remove deletes it.
            options.highlighted = 0
            await pilot.pause(0.05)
            assert not screen.query_one("#rules-remove", Button).disabled
            options.action_select()
            await pilot.pause(0.05)
            assert screen.query_one("#rules-target", Input).value == "slack.desktop"
            screen.remove_rule()
            await _until(pilot, lambda: ("remove", "com.slack") in backend.requests)
            await _until(pilot, lambda: options.option_count == 1)

            screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, NotificationRulesScreen))

    _run(scenario())


def test_rules_panel_notes_when_rules_do_not_apply() -> None:
    async def scenario() -> None:
        backend = _Backend(policy="messages")
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            app.set_focus(None)
            app.action_click_rules()
            await _until(pilot, lambda: isinstance(app.screen, NotificationRulesScreen))
            await _until(pilot, lambda: bool(app.screen.query("#rules-policy-note")))
            note = app.screen.query_one("#rules-policy-note", Static)
            assert POLICY_NOTE in str(note.render())

    _run(scenario())


def test_a_pending_save_preserves_a_new_draft(monkeypatch) -> None:
    async def scenario() -> None:
        backend = _Backend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            app.set_focus(None)
            app.action_click_rules()
            await _until(pilot, lambda: isinstance(app.screen, NotificationRulesScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.rules == backend.rules)
            pending = []
            monkeypatch.setattr(screen, "_run", lambda action, done: pending.append((action, done)))
            bundle = screen.query_one("#rules-bundle", Input)
            target = screen.query_one("#rules-target", Input)
            bundle.value, target.value = "com.example.first", "https://first.example/"
            screen.save_rule()

            bundle.value, target.value = "com.example.second", "https://second.example/"
            action, done = pending.pop()
            done(action())

            assert ("set", "com.example.first", "https://first.example/") in backend.requests
            assert bundle.value == "com.example.second"
            assert target.value == "https://second.example/"

    _run(scenario())


def test_rules_key_explains_an_older_backend() -> None:
    async def scenario() -> None:
        backend = _Backend(supported=False)
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        notices = []
        app.notify = lambda message, **_kwargs: notices.append(message)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            app.set_focus(None)
            app.action_click_rules()
            await pilot.pause(0.1)
            assert not isinstance(app.screen, NotificationRulesScreen)
            assert backend.requests == []
            assert any("restart it after upgrading" in notice for notice in notices)

    _run(scenario())


def test_rule_text_is_never_parsed_as_markup() -> None:
    async def scenario() -> None:
        backend = _Backend()
        backend.rules = [{
            "bundle_id": "com.evil", "kind": "url",
            "target": "https://x.example/[@click=app.quit]x[/]",
        }]
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            app.set_focus(None)
            app.action_click_rules()
            await _until(pilot, lambda: isinstance(app.screen, NotificationRulesScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.rules == backend.rules)
            prompt = screen.query_one("#rules-list", OptionList).get_option_at_index(0).prompt
            assert "[@click=app.quit]" in str(prompt)
            assert app.is_running

    _run(scenario())
