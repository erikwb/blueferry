"""Recent iPhone notifications in the Textual client, driven by fakes only."""
from __future__ import annotations

import threading

from textual.widgets import Button, Static, TabbedContent

from blueferry.client import BackendError
from blueferry.models import BackendStatus
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_notifications import NotificationsScreen
from blueferry.tui_options import OptionsScreen
from tests.test_tui import _Backend, _run_headless


class _NotificationsBackend(_Backend):
    def __init__(self, **extra) -> None:
        super().__init__()
        self.extra = {"notification_history_enabled": True, **extra}
        self.fetches: list[int] = []
        self.content = False
        self.records = [{
            "app_name": "[@click=app.quit]Mail[/]", "title": "[b]Hi[/b]",
            "subtitle": "Ana", "body": "secret\x1b[2J", "time": "2026-10-27T09:00:00+01:00",
        }]
        self.error: str | None = None
        self.gate: threading.Event | None = None

    def status(self) -> BackendStatus:  # type: ignore[override]
        return BackendStatus.from_dict(
            {"daemon": True, "map": True, "pbap": True, "ancs": True, **self.extra}
        )

    def notifications(self, limit: int = 50) -> dict:
        if self.gate is not None:
            self.gate.wait(5)
        self.fetches.append(limit)
        if self.error:
            raise BackendError(self.error)
        enabled = self.extra.get("notification_history_enabled") is True
        return {
            "enabled": enabled, "content": self.content,
            "notifications": self.records if enabled else [],
        }

    def features(self) -> dict:
        return {}


class _Monitor:
    def __init__(self) -> None:
        self.changed = 0

    def pump(self):
        return False, None

    def take_notifications_changed(self) -> bool:
        changed, self.changed = self.changed > 0, 0
        return changed

    def close(self) -> None:
        pass


async def _until(pilot, condition) -> None:
    for _attempt in range(60):
        if condition():
            return
        await pilot.pause(0.05)
    assert condition()


def _text(screen, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())


async def _open(app, pilot) -> NotificationsScreen:
    await _until(pilot, lambda: app.state.status.daemon)
    app.action_notifications()
    await _until(pilot, lambda: isinstance(app.screen, NotificationsScreen)
                 and bool(app.screen.query("#notifications-list")))
    return app.screen


def test_lists_notifications_as_plain_text_and_forgets_them_on_close() -> None:
    backend = _NotificationsBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(pilot, lambda: "Mail" in _text(screen, "#notifications-list"))
            listing = _text(screen, "#notifications-list")
            assert "[@click=app.quit]Mail[/]" in listing
            assert "[b]Hi[/b] · Ana" in listing and "\x1b" not in listing
            # Only app names and times would be kept: say where to change it.
            assert "Settings \u203a Notifications" in _text(screen, "#notifications-content-hint")
            assert screen.query_one("#notifications-settings", Button).display
            assert backend.fetches == [100]

            screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, NotificationsScreen))
            assert screen.history.rows == []
            assert not screen.history.watched

    _run_headless(scenario())


def test_notifications_changed_reloads_once_and_only_while_open() -> None:
    backend = _NotificationsBackend()
    monitor = _Monitor()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: monitor)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            monitor.changed = 3
            await pilot.pause(0.5)
            assert backend.fetches == []  # closed: nothing is read

            screen = await _open(app, pilot)
            await _until(pilot, lambda: backend.fetches == [100])
            monitor.changed = 2  # the D-Bus signal, through the monitor
            await _until(pilot, lambda: len(backend.fetches) >= 2)
            await pilot.pause(0.4)
            assert len(backend.fetches) == 2
            for _ in range(5):  # a burst coalesces into one reload
                screen.invalidated()
            await _until(pilot, lambda: len(backend.fetches) >= 3)
            await pilot.pause(0.4)
            assert len(backend.fetches) == 3

            screen.action_close()
            await _until(pilot, lambda: not isinstance(app.screen, NotificationsScreen))
            monitor.changed = 1
            await pilot.pause(0.5)
            assert len(backend.fetches) == 3

    _run_headless(scenario())


def test_a_late_reply_after_closing_is_dropped() -> None:
    backend = _NotificationsBackend()
    backend.gate = threading.Event()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.action_close()
            await _until(pilot, lambda: not screen.is_attached)
            backend.gate.set()
            await _until(pilot, lambda: backend.fetches == [100])
            await pilot.pause(0.2)
            assert screen.history.rows == []
            assert app.is_running

    _run_headless(scenario())


def test_switched_off_list_points_to_the_notification_options() -> None:
    backend = _NotificationsBackend(notification_history_enabled=False)

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(
                pilot, lambda: "Is Off" in _text(screen, "#notifications-placeholder-title"),
            )
            explanation = _text(screen, "#notifications-explanation")
            assert "Keep recent iPhone notifications" in explanation
            assert "Settings \u203a Notifications" in explanation
            assert screen.query_one("#notifications-refresh", Button).disabled

            # Switched on while open: the next status reloads the list.
            backend.extra["notification_history_enabled"] = True
            app._load_data()
            await _until(pilot, lambda: "Mail" in _text(screen, "#notifications-list"))
            assert not screen.query_one("#notifications-refresh", Button).disabled
            backend.extra["notification_history_enabled"] = False
            app._load_data()
            await _until(pilot, lambda: screen.query_one("#notifications-settings").display)

            screen.query_one("#notifications-settings", Button).press()
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen)
                         and bool(app.screen.query("#options-tabs")))
            tabs = app.screen.query_one("#options-tabs", TabbedContent)
            await _until(pilot, lambda: tabs.active == "options-notifications")
            assert screen.history.rows == []

    _run_headless(scenario())


def test_a_failing_list_explains_itself_as_plain_text() -> None:
    backend = _NotificationsBackend()
    backend.error = "[@click=app.quit]boom[/]"

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(
                pilot,
                lambda: "Unavailable" in _text(screen, "#notifications-placeholder-title"),
            )
            assert "[@click=app.quit]boom[/]" in _text(screen, "#notifications-explanation")
            backend.error = None
            await pilot.press("r")
            await _until(pilot, lambda: "Mail" in _text(screen, "#notifications-list"))

    _run_headless(scenario())


def test_palette_opens_the_phone_screens() -> None:
    backend = _NotificationsBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _until(pilot, lambda: app.state.status.daemon)
            commands = {command.title: command for command in app.get_system_commands(app.screen)}
            for title in ("iPhone Overview", "iPhone Notifications", "Phone Calls", "Recent Calls"):
                assert title in commands
            commands["iPhone Notifications"].callback()
            await _until(pilot, lambda: isinstance(app.screen, NotificationsScreen))
            # Opening it twice does not stack a second list.
            commands["iPhone Notifications"].callback()
            await pilot.pause(0.1)
            assert sum(isinstance(s, NotificationsScreen) for s in app.screen_stack) == 1

    _run_headless(scenario())


def test_event_monitor_subscribes_to_notifications_changed(monkeypatch) -> None:
    from blueferry import tui as tui_module

    receivers: dict[str, object] = {}

    class _Match:
        def remove(self) -> None:
            pass

    class _Bus:
        def add_signal_receiver(self, handler, *, signal_name, **_kwargs):
            receivers[signal_name] = handler
            return _Match()

    monkeypatch.setattr(tui_module, "get_session_bus", lambda: _Bus())
    monitor = tui_module._EventMonitor()
    assert not monitor.take_notifications_changed()
    receivers["NotificationsChanged"]()
    receivers["NotificationsChanged"]()
    assert monitor.take_notifications_changed()
    assert not monitor.take_notifications_changed()
    monitor.close()
