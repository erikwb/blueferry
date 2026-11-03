"""Recent iPhone notifications in the Textual client, driven by fakes only."""
from __future__ import annotations

import threading

from rich.text import Text
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
        self.extra = {
            "notification_history_enabled": True, "storage_state": "ready", **extra,
        }
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

    def take_tether_changed(self) -> bool:
        return False

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
            await _until(pilot, lambda: "Mail" in screen.body_text)
            listing = screen.body_text
            assert "[@click=app.quit]Mail[/]" in listing
            # Content hidden: no title, subtitle or body, only app and time.
            assert "Hi" not in listing and "Ana" not in listing and "secret" not in listing
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
            await _until(pilot, lambda: "Mail" in screen.body_text)
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
            await _until(pilot, lambda: "Mail" in screen.body_text)

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


# ---- pins and muted apps ------------------------------------------------------


class _PinBackend(_NotificationsBackend):
    def __init__(self, **extra) -> None:
        super().__init__(**extra)
        self.pins_max = 20
        self.records = [
            {"key": "k1", "app_id": "com.example.mail", "app_name": "Mail",
             "title": "Pinned one", "time": "2026-10-27T09:00:00+01:00",
             "pinned": True, "removed": True, "actionable": False},
            {"key": "k2", "app_id": "com.example.chat", "app_name": "[b]Chat[/b]",
             "title": "Hello", "time": "2026-10-27T10:00:00+01:00",
             "pinned": False, "removed": False, "actionable": True},
        ]
        self.pinned: list[tuple[str, bool]] = []
        self.pin_error: str | None = None
        self.pin_error_name = "org.blueferry.Error.InvalidArgs"
        self.apps_error: str | None = None
        self.apps = {"apps": [{"app_id": "com.example.chat", "muted": True}],
                     "mute_available": True}
        self.app_loads = 0

    def notifications(self, limit: int = 50) -> dict:
        snapshot = super().notifications(limit)
        snapshot["pins_max"] = self.pins_max
        return snapshot

    def set_notification_pinned(self, key: str, pinned: bool) -> bool:
        self.pinned.append((key, pinned))
        if self.pin_error:
            raise BackendError(self.pin_error, dbus_name=self.pin_error_name)
        for record in self.records:
            if record["key"] == key:
                record["pinned"] = pinned
        return pinned

    def notification_apps(self) -> dict:
        self.app_loads += 1
        if self.apps_error:
            raise BackendError(self.apps_error)
        return self.apps


def _items(screen) -> list:
    from blueferry.tui_notifications import NotificationItem

    return list(screen.query(NotificationItem))


async def _highlight(pilot, screen, index: int) -> None:
    from textual.widgets import ListView

    screen.query_one("#notifications-list", ListView).index = index
    item = _items(screen)[index]
    await _until(pilot, lambda: screen._selected() is item)


async def _listed(pilot, screen) -> None:
    """Wait until both rows are listed and the render worker has highlighted
    the first one; ``extend`` returns before the index and the pin button are
    set."""
    await _until(pilot, lambda: len(_items(screen)) == 2)
    await _until(pilot, lambda: screen._selected() is _items(screen)[0])


def test_pin_marks_and_dims_rows_and_pins_the_highlighted_one() -> None:
    backend = _PinBackend()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _listed(pilot, screen)
            first, second = _items(screen)
            body = screen.body_text
            assert "Pinned  ·  Mail" in body and "Removed on iPhone" in body
            assert "[b]Chat[/b]" in body
            # Removed on the iPhone: dimmed. Muting needs daemon support.
            assert first.has_class("-dimmed") and not second.has_class("-dimmed")
            assert backend.app_loads == 0
            pin = screen.query_one("#notifications-pin", Button)
            await _until(pilot, lambda: pin.display and str(pin.label) == "Unpin  P")
            assert isinstance(pin.tooltip, Text)

            await _highlight(pilot, screen, 1)
            await _until(pilot, lambda: str(pin.label) == "Pin  P")
            screen.query_one("#notifications-list").focus()
            await pilot.press("P")
            await _until(pilot, lambda: backend.pinned == [("k2", True)])
            await _until(pilot, lambda: "Pinned  ·  [b]Chat[/b]" in screen.body_text)
            # The highlight follows the notification across the reload.
            await _until(pilot, lambda: str(pin.label) == "Unpin  P")

    _run_headless(scenario())


def test_pin_limit_disables_pin_but_not_unpin() -> None:
    backend = _PinBackend()
    backend.pins_max = 1

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _listed(pilot, screen)
            pin = screen.query_one("#notifications-pin", Button)
            await _until(pilot, lambda: not pin.disabled)  # the pinned row can unpin
            await _highlight(pilot, screen, 1)
            await _until(pilot, lambda: pin.disabled)
            await _until(
                pilot, lambda: "At most 1 notifications" in _text(screen, "#notifications-pin-state"),
            )
            screen.action_toggle_pin()
            await pilot.pause(0.1)
            assert backend.pinned == []

    _run_headless(scenario())


def test_a_failed_pin_explains_itself_from_the_dbus_error_name() -> None:
    from blueferry import notification_pin_view

    backend = _PinBackend()
    backend.pin_error = "at most 20 notifications can be pinned; unpin one first"

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _listed(pilot, screen)
            await _highlight(pilot, screen, 1)
            screen.action_toggle_pin()
            await _until(
                pilot, lambda: "pinned list is full" in _text(screen, "#notifications-pin-state"),
            )
            # The error is about that row: moving away clears it, and it does
            # not come back.
            await _highlight(pilot, screen, 0)
            await _until(pilot, lambda: screen.pin_error == "")
            await _until(
                pilot,
                lambda: "pinned list is full" not in _text(screen, "#notifications-pin-state"),
            )

            # The name decides, not a word in the message: a NotReady error
            # is "locked" even without that word, and "locked" in an
            # InvalidArgs message is no lock.
            backend.pin_error = "storage unavailable"
            backend.pin_error_name = "org.blueferry.Error.NotReady"
            await _highlight(pilot, screen, 1)
            screen.action_toggle_pin()
            await _until(pilot, lambda: screen.pin_error == notification_pin_view.LOCKED_TEXT)

            backend.pin_error = "[b]the key is locked to a session[/b]"
            backend.pin_error_name = "org.blueferry.Error.InvalidArgs"
            screen.action_toggle_pin()
            await _until(
                pilot, lambda: screen.pin_error == notification_pin_view.GENERIC_ERROR_TEXT,
            )
            # A reload asked for by the user clears it too.
            await pilot.pause(0.1)
            screen.action_refresh()
            assert screen.pin_error == ""

            backend.pin_error = "[b]cannot pin[/b]"
            backend.pin_error_name = "org.freedesktop.DBus.Error.UnknownMethod"
            await pilot.pause(0.1)
            screen.action_toggle_pin()
            await _until(
                pilot, lambda: screen.pin_error == notification_pin_view.UNSUPPORTED_TEXT,
            )

    _run_headless(scenario())


def test_a_pin_refused_while_settings_json_cannot_be_read_says_so() -> None:
    from blueferry import notification_pin_view

    backend = _PinBackend()
    # The daemon refuses pin and unpin with Configuration and saves nothing.
    backend.pin_error = (
        "settings.json can't be read, so local storage was not changed; "
        "repair or remove the file and try again"
    )
    backend.pin_error_name = "io.weirdware.BlueFerry.Error.Configuration"

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _listed(pilot, screen)
            for index, pinned in ((1, False), (0, True)):
                await _highlight(pilot, screen, index)
                screen.action_toggle_pin()
                await _until(
                    pilot, lambda: screen.pin_error == notification_pin_view.CONFIGURATION_TEXT,
                )
                await _until(pilot, lambda: not screen.pinning)
                assert "settings.json can't be read" in _text(
                    screen, "#notifications-pin-state",
                )
                # The row keeps the daemon's state: nothing was pinned or unpinned.
                assert _items(screen)[index].record["pinned"] is pinned
            assert backend.pinned == [("k2", True), ("k1", False)]

    _run_headless(scenario())


def test_a_failed_app_list_keeps_muted_rows_dimmed() -> None:
    backend = _PinBackend(notification_muted_app_count=1)
    monitor = _AppsMonitor()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: monitor)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(pilot, lambda: len(_items(screen)) == 2
                         and _items(screen)[1].has_class("-dimmed"))
            backend.apps_error = "storage locked"
            monitor.apps_changed = True
            await _until(pilot, lambda: backend.app_loads == 2)
            await pilot.pause(0.2)
            assert screen.muted == frozenset({"com.example.chat"})
            assert _items(screen)[1].has_class("-dimmed")

    _run_headless(scenario())


def test_rows_of_muted_apps_are_dimmed_and_follow_notification_apps_changed() -> None:
    backend = _PinBackend(notification_muted_app_count=1)
    monitor = _AppsMonitor()

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: monitor)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(pilot, lambda: len(_items(screen)) == 2
                         and _items(screen)[1].has_class("-dimmed"))
            backend.apps = {"apps": [], "mute_available": True}
            monitor.apps_changed = True
            await _until(pilot, lambda: len(_items(screen)) == 2
                         and not _items(screen)[1].has_class("-dimmed"))
            assert backend.app_loads == 2

    _run_headless(scenario())


def test_without_pin_keys_the_pin_button_stays_hidden() -> None:
    backend = _NotificationsBackend()  # an older daemon: no keys

    async def scenario() -> None:
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await _until(pilot, lambda: len(_items(screen)) == 1)
            await _until(pilot, lambda: screen._selected() is _items(screen)[0])
            assert not screen.query_one("#notifications-pin", Button).display
            screen.action_toggle_pin()  # nothing to pin, nothing happens
            await pilot.pause(0.1)

    _run_headless(scenario())


class _AppsMonitor(_Monitor):
    def __init__(self) -> None:
        super().__init__()
        self.apps_changed = False

    def take_notification_apps_changed(self) -> bool:
        changed, self.apps_changed = self.apps_changed, False
        return changed


def test_event_monitor_subscribes_to_notification_apps_changed(monkeypatch) -> None:
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
    assert not monitor.take_notification_apps_changed()
    receivers["NotificationAppsChanged"]()
    assert monitor.take_notification_apps_changed()
    assert not monitor.take_notification_apps_changed()
    monitor.close()
