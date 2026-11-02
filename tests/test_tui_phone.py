"""Opt-in now playing and recent calls in the Textual client (fakes only)."""
from __future__ import annotations

import threading

from rich.text import Text
from textual.widgets import Button, Input, Static

from blueferry import phone_overview as overview
from blueferry.client import BackendError
from blueferry.models import BackendStatus, CallHistoryEntry, CallsSnapshot
from blueferry.tui import BlueFerryApp, NewMessageScreen, TuiState
from blueferry.tui_calls import CallsScreen, DialConfirmScreen
from blueferry.tui_notifications import NotificationsScreen
from blueferry.tui_phone import (
    CallGroupItem,
    PhoneOverviewScreen,
    RecentCallsScreen,
    now_playing_text,
)
from tests.test_tui import _Backend, _run_headless


def _status(**extra) -> BackendStatus:
    return BackendStatus.from_dict(
        {"daemon": True, "map": True, "pbap": True, "ancs": True, **extra}
    )


class _PhoneBackend(_Backend):
    def __init__(self, **extra) -> None:
        super().__init__()
        self.extra = extra
        self.snapshot = {
            "enabled": True,
            "available": True,
            "player": {"state": "playing"},
            "track": {"title": "[b]Song[/b]", "artist": "Band\x1b[31m"},
            "supported_commands": ["pause", "previous"],
        }
        self.now_playing_calls = 0
        self.commands: list[str] = []
        self.history_calls: list[int] = []
        self.history: list[CallHistoryEntry] = [
            CallHistoryEntry("missed", "2026-10-01T10:00:00+02:00", "+41790000000",
                             name="[@click=app.quit]Ana[/]"),
            CallHistoryEntry("outgoing", "2026-10-01T09:00:00+02:00", "+41790000001"),
        ]

    def status(self) -> BackendStatus:  # type: ignore[override]
        return _status(**self.extra)

    def now_playing(self) -> dict:
        self.now_playing_calls += 1
        return self.snapshot

    def send_media_command(self, command: str) -> None:
        self.commands.append(command)

    def call_history(self, limit: int = 200) -> list[CallHistoryEntry]:
        self.history_calls.append(limit)
        return self.history


async def _until(pilot, condition) -> None:
    for _attempt in range(40):
        if condition():
            return
        await pilot.pause(0.05)
    assert condition()


def test_now_playing_text_renders_iphone_text_as_plain_text() -> None:
    view = overview.now_playing({
        "available": True,
        "player": {"state": "paused"},
        "track": {"title": "[b]x[/b]\nnext"},
        "supported_commands": ["play", "next"],
    })
    text = now_playing_text(view)
    assert text.plain == "⏸  [b]x[/b] next   p ]"
    assert now_playing_text(overview.now_playing(None)).plain == ""


def test_media_line_and_keys_follow_the_opt_in_and_supported_commands() -> None:
    backend = _PhoneBackend(media_control_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            line = app.query_one("#now-playing")
            await _until(pilot, lambda: line.display)
            assert "[b]Song[/b] — Band" in line.render().plain
            assert "\x1b" not in line.render().plain

            await pilot.press("p")
            await pilot.press("left_square_bracket")
            await _until(pilot, lambda: len(backend.commands) == 2)
            # The phone does not offer next, so nothing is sent.
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            assert backend.commands == ["toggle", "previous"]

    _run_headless(scenario())


class _MediaMonitor:
    def __init__(self) -> None:
        self.pending = False

    def pump(self):
        return False, None

    def take_now_playing_changed(self) -> bool:
        pending, self.pending = self.pending, False
        return pending

    def take_tether_changed(self) -> bool:
        return False

    def close(self) -> None:
        pass


def test_status_refresh_does_not_refetch_now_playing() -> None:
    backend = _PhoneBackend(media_control_enabled=True)
    monitor = _MediaMonitor()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: monitor)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: backend.now_playing_calls == 1)
            # A changed status (here: a battery reading) is no reason to fetch.
            backend.extra["phone_battery"] = 40
            await pilot.press("r")
            await _until(pilot, lambda: app.state.status.extra.get("phone_battery") == 40)
            await pilot.pause(0.2)
            assert backend.now_playing_calls == 1

            # NowPlayingChanged is.
            monitor.pending = True
            await _until(pilot, lambda: backend.now_playing_calls == 2)

            # So is the opt-in switching, which the daemon also signals.
            backend.extra["media_control_enabled"] = False
            await pilot.press("r")
            await _until(pilot, lambda: app.query_one("#now-playing").display is False)
            backend.extra["media_control_enabled"] = True
            await pilot.press("r")
            await _until(pilot, lambda: backend.now_playing_calls == 3)

    _run_headless(scenario())


def test_media_stays_hidden_and_silent_without_the_opt_in() -> None:
    backend = _PhoneBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("p")
            await pilot.pause(0.1)
            assert app.query_one("#now-playing").display is False
            assert backend.now_playing_calls == 0
            assert backend.commands == []

    _run_headless(scenario())


def test_media_failure_hides_the_line() -> None:
    class Failing(_PhoneBackend):
        def now_playing(self) -> dict:
            raise BackendError("no media service")

    backend = Failing(media_control_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            assert app.query_one("#now-playing").display is False
            assert app.now_playing.visible is False

    _run_headless(scenario())


def test_recent_calls_screen_lists_calls_as_plain_text() -> None:
    backend = _PhoneBackend(call_history_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            assert isinstance(app.screen, RecentCallsScreen)
            screen = app.screen
            await _until(pilot, lambda: "Ana" in screen.body_text)
            plain = screen.body_text
            assert "[@click=app.quit]Ana[/]  Missed · +41790000000" in plain
            assert "+41790000001  Outgoing" in plain
            # Day sections from call_groups; rows carry the direction label.
            assert screen.groups[0]["day"] in plain.splitlines()
            rows = list(screen.query(CallGroupItem))
            assert str(rows[0].tooltip).startswith("Missed, [@click=app.quit]Ana[/], ")
            assert backend.history_calls == [100]

            await pilot.press("m")
            await _until(pilot, lambda: "+41790000001" not in screen.body_text)
            assert "Ana" in screen.body_text
            await pilot.press("escape")
            assert not isinstance(app.screen, RecentCallsScreen)

    _run_headless(scenario())


def test_recent_calls_buttons_fit_an_80_column_terminal() -> None:
    backend = _PhoneBackend(call_history_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(80, 24)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: "Ana" in screen.body_text)
            await pilot.pause(0.05)
            dialog = screen.query_one("#recent-calls-dialog").region
            for button in screen.query(Button):
                assert dialog.contains_region(button.region), button.id

    _run_headless(scenario())


def test_recent_calls_need_the_opt_in() -> None:
    backend = _PhoneBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("h")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, RecentCallsScreen)
            assert backend.history_calls == []

    _run_headless(scenario())


def test_late_media_reply_stays_hidden_after_opt_out(monkeypatch) -> None:
    backend = _PhoneBackend(media_control_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.now_playing.visible)
            pending = []
            monkeypatch.setattr(app, "_fetch_now_playing", pending.append)
            app._load_now_playing()
            generation = pending[0]
            for _ in range(1000):
                app._load_now_playing()
            assert len(pending) == 1
            backend.extra["media_control_enabled"] = False
            await pilot.press("r")
            await _until(pilot, lambda: not app.query_one("#now-playing").display)
            app._media_loaded(overview.now_playing(backend.snapshot), generation)
            assert not app.now_playing.visible
            assert not app.query_one("#now-playing").display
            assert len(pending) == 1

    _run_headless(scenario())


def test_media_burst_coalesces_and_ignores_the_superseded_reply(monkeypatch) -> None:
    backend = _PhoneBackend(media_control_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.now_playing.visible)
            pending = []
            monkeypatch.setattr(app, "_fetch_now_playing", pending.append)
            original = app.now_playing
            for _ in range(1000):
                app._load_now_playing()
            assert len(pending) == 1
            app._media_loaded(overview.now_playing(None), pending[0])
            assert app.now_playing == original
            assert len(pending) == 2
            app._media_loaded(overview.now_playing(None), pending[1])
            assert not app.now_playing.visible

    _run_headless(scenario())


def test_disabling_call_history_closes_the_open_history_screen() -> None:
    backend = _PhoneBackend(call_history_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            assert isinstance(app.screen, RecentCallsScreen)
            backend.extra["call_history_enabled"] = False
            app._load_data()
            await _until(pilot, lambda: not isinstance(app.screen, RecentCallsScreen))

    _run_headless(scenario())


class _CallingBackend(_PhoneBackend):
    def __init__(self, **extra) -> None:
        super().__init__(**extra)
        self.dialed: list[str] = []

    def calls(self) -> CallsSnapshot:
        return CallsSnapshot(state="ready")

    def dial(self, number: str) -> str:
        self.dialed.append(number)
        return "voicecall01"

    def find_contacts(self, query: str) -> list[tuple[str, str]]:
        return []

    def notifications(self, limit: int = 50) -> dict:
        return {"enabled": False, "content": False, "notifications": []}


def _overview_shown(app) -> bool:
    screen = app.screen
    return isinstance(screen, PhoneOverviewScreen) and bool(screen.query("#phone-overview-name"))


def _overview_text(screen) -> str:
    return "\n".join(
        str(screen.query_one(selector, Static).render())
        for selector in (
            "#phone-overview-name", "#phone-overview-connection",
            "#phone-overview-status", "#phone-overview-media", "#phone-overview-hints",
        )
    )


def test_overview_shows_the_phone_card_and_names_the_settings() -> None:
    backend = _CallingBackend(
        phone_battery_level=80, phone_network_name="Swiss[b]com[/b]",
        media_control_enabled=True,
    )

    async def scenario() -> None:
        app = BlueFerryApp(
            TuiState(backend), monitor_factory=lambda: None,
            phone_name=lambda: "[@click=app.quit]Josh's iPhone[/]",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.now_playing.visible)
            app.action_phone_overview()
            await _until(pilot, lambda: _overview_shown(app))
            screen = app.screen
            await _until(pilot, lambda: "Josh" in _overview_text(screen))
            text = _overview_text(screen)
            assert "[@click=app.quit]Josh's iPhone[/]" in text
            assert "Connected" in text
            assert "Battery 80 %" in text and "Network Swiss[b]com[/b]" in text
            assert "[b]Song[/b] — Band" in text
            # Off features name the switch and the settings category.
            assert "Phone calls: Turn on" in text and "Calls & Audio" in text
            assert "Recent calls: Turn on" in text
            assert "Notification list: Turn on" in text
            assert screen.query_one("#phone-overview-calls", Button).disabled
            assert screen.query_one("#phone-overview-recent", Button).disabled

            # A status update while it is open: calls and history switched on.
            backend.extra.update(calls_enabled=True, call_history_enabled=True)
            app._load_data()
            await _until(pilot, lambda: "Phone calls:" not in _overview_text(screen))
            assert not screen.query_one("#phone-overview-calls", Button).disabled
            assert "Recent calls:" not in _overview_text(screen)

            screen.query_one("#phone-overview-notifications", Button).press()
            await _until(pilot, lambda: isinstance(app.screen, NotificationsScreen))
            app.screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, NotificationsScreen))

            # The name is read once and kept.
            app.action_phone_overview()
            await _until(pilot, lambda: _overview_shown(app))
            await _until(pilot, lambda: "Josh" in _overview_text(app.screen))

    _run_headless(scenario())


def test_a_late_phone_name_after_closing_the_overview_is_dropped() -> None:
    backend = _CallingBackend()
    release = threading.Event()
    started = threading.Event()

    def slow_name() -> str:
        started.set()
        release.wait(5)
        return "Late"

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None, phone_name=slow_name)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.action_phone_overview()
            await _until(pilot, started.is_set)
            screen = app.screen
            screen.action_close()
            await _until(pilot, lambda: not screen.is_attached)
            release.set()
            await pilot.pause(0.3)
            assert app.is_running
            assert screen.phone_name == "iPhone"
            assert app._phone_name == ""  # read again next time

    _run_headless(scenario())


def test_a_failed_phone_name_read_is_tried_again_next_time() -> None:
    backend = _CallingBackend()
    answers = ["", "Josh's iPhone"]
    reads = []

    def name() -> str:
        reads.append(1)
        return answers[len(reads) - 1]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None, phone_name=name)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.action_phone_overview()
            await _until(pilot, lambda: _overview_shown(app) and len(reads) == 1)
            await pilot.pause(0.2)
            assert app.screen.phone_name == "iPhone" and app._phone_name == ""
            app.screen.action_close()
            await _until(pilot, lambda: not _overview_shown(app))
            app.action_phone_overview()
            await _until(pilot, lambda: _overview_shown(app))
            await _until(pilot, lambda: "Josh" in _overview_text(app.screen))
            assert len(reads) == 2 and app._phone_name == "Josh's iPhone"

    _run_headless(scenario())


def test_call_back_confirms_and_message_prefills_the_recipient() -> None:
    backend = _CallingBackend(
        call_history_enabled=True, calls_enabled=True, calls_state="ready",
    )

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: bool(list(screen.query(CallGroupItem))))
            await _until(
                pilot, lambda: not screen.query_one("#recent-calls-call", Button).disabled,
            )
            screen.call_back()
            await _until(pilot, lambda: isinstance(app.screen, DialConfirmScreen)
                         and bool(app.screen.query("#calls-confirm-call")))
            assert app.screen.number == "+41790000000"
            app.screen.call_button()
            await _until(pilot, lambda: backend.dialed == ["+41790000000"])
            assert isinstance(app.screen, CallsScreen)
            app.screen.action_close()

            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: bool(list(screen.query(CallGroupItem))))
            await _until(
                pilot, lambda: not screen.query_one("#recent-calls-message", Button).disabled,
            )
            screen.message()
            await _until(pilot, lambda: isinstance(app.screen, NewMessageScreen))
            await pilot.pause(0.1)
            assert app.screen.query_one("#new-recipient", Input).value == "+41790000000"

    _run_headless(scenario())


def test_call_back_waits_for_calls_to_be_ready() -> None:
    backend = _CallingBackend(call_history_enabled=True)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: bool(list(screen.query(CallGroupItem))))
            await pilot.pause(0.1)
            button = screen.query_one("#recent-calls-call", Button)
            assert button.disabled
            assert "Calls & Audio" in str(button.tooltip)
            screen.call_back()
            await pilot.pause(0.1)
            assert isinstance(app.screen, RecentCallsScreen)
            assert backend.dialed == []

    _run_headless(scenario())


def test_hostile_caller_names_in_row_tooltips_are_plain_text() -> None:
    backend = _CallingBackend(call_history_enabled=True)
    backend.history = [
        CallHistoryEntry("missed", "2026-10-01T10:00:00+02:00", "+41790000000",
                         name="Bob [/nope]"),
        CallHistoryEntry("incoming", "2026-10-01T09:00:00+02:00", "+41790000001",
                         name="[@click=app.quit]Ana[/]"),
    ]

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40), tooltips=True) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: len(list(screen.query(CallGroupItem))) == 2)
            for item in screen.query(CallGroupItem):
                assert isinstance(item.tooltip, Text)
                await pilot.hover(item)
                await pilot.pause(0.8)
                assert app.is_running
                tooltip = app.screen.query_one("#textual-tooltip")
                rendered = tooltip.render()
                plain = getattr(rendered, "plain", str(rendered))
                assert ("Bob [/nope]" in plain) or ("[@click=app.quit]Ana[/]" in plain)
                spans = getattr(rendered, "spans", [])
                assert all("@click" not in str(span) for span in spans)
            assert isinstance(app.screen, RecentCallsScreen)

    _run_headless(scenario())


def test_refresh_from_iphone_syncs_once_and_reports_failures_as_plain_text() -> None:
    backend = _CallingBackend(call_history_enabled=True)
    release = threading.Event()
    syncs: list[int] = []

    def sync() -> int:
        syncs.append(1)
        release.wait(5)
        if len(syncs) > 1:
            raise BackendError("[@click=app.quit]boom[/]")
        return 3

    backend.sync_call_history = sync

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.extra.get("call_history_enabled"))
            await pilot.press("h")
            screen = app.screen
            await _until(pilot, lambda: screen.loaded)
            button = screen.query_one("#recent-calls-sync", Button)
            await pilot.press("s")
            screen.sync_button()  # a second press while syncing does nothing
            await _until(pilot, lambda: button.disabled)
            assert "Reading" in str(screen.query_one("#recent-calls-sync-state", Static).render())
            release.set()
            await _until(pilot, lambda: not button.disabled)
            assert syncs == [1]
            await _until(pilot, lambda: len(backend.history_calls) >= 2)
            assert backend.starred == []  # "s" did not star a conversation

            screen.action_sync()
            await _until(pilot, lambda: bool(screen.sync_error))
            await _until(pilot, lambda: not button.disabled)
            state = screen.query_one("#recent-calls-sync-state", Static).render()
            assert "Call history sync failed: [@click=app.quit]boom[/]" in str(state)
            assert app.is_running

    _run_headless(scenario())
