"""The terminal client's Options screen: categories, saved preferences,
feature switches with "Restart Service", per-key actions and diagnostics."""
from __future__ import annotations

import asyncio
import threading

import pytest
from textual.widgets import Button, Checkbox, Select, Static, TabbedContent, TextArea

from blueferry import tui_doctor
from blueferry.client import BackendError
from blueferry.doctor_report import DoctorReport
from blueferry.models import BackendStatus
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_doctor import DoctorScreen
from blueferry.tui_notification_rules import NotificationRulesScreen
from blueferry.tui_options import (
    FEATURE_ROWS,
    GROUPS,
    SAVED_SWITCHES,
    OptionsScreen,
    categories,
)
from tests.test_tui import _Backend

_FULL_EXTRA = {
    "profile_reset_auto": False,
    "phone_reconnect_state": "waiting",
    "phone_reconnect_supported": True,
    "ancs_actions_preference": False,
    "mirror_iphone_removals": False,
    "call_history_enabled": False,
    "missed_call_notifications": True,
    "phone_battery_warning_percent": 15,
    "media_control_enabled": False,
    "media_mpris_enabled": False,
    "proximity_lock": "off",
    "proximity_lock_enabled": False,
    "proximity_lock_grace_sec": 90,
    "phone_audio_route": "phone",
    "notification_open_map_revision": 1,
}


def _feature(value: bool, *, running: bool | None = None, source: str = "settings",
             variable: str = "") -> dict:
    return {
        "value": value, "running": value if running is None else running,
        "source": source, "variable": variable,
        "restart_required": running is not None and running != value,
    }


class _OptionsBackend(_Backend):
    def __init__(
        self, *, photos: bool | None = False, otp: bool | None = False,
        saved_opt_ins: bool = True, fail: bool = False, extra: dict | None = None,
        features: dict | None = None, policy: str = "all",
    ) -> None:
        super().__init__()
        self.photos = photos
        self.otp = otp
        self.fail = fail
        self.saved_opt_ins = saved_opt_ins
        self.extra = dict(_FULL_EXTRA if extra is None else extra)
        self.feature_items = features
        self.policy = policy
        self.battery = False
        self.saved: list[bool] = []
        self.calls: list[tuple] = []
        self.feature_loads = 0

    def status(self) -> BackendStatus:  # type: ignore[override]
        return BackendStatus(
            daemon=True, map=True, pbap=True, ancs=True,
            contact_photos=self.photos, otp_autocopy=self.otp,
            saved_opt_ins=self.saved_opt_ins, notification_policy=self.policy,
            phone_battery_warning=self.battery, storage_policy="encrypted",
            storage_state="ready", extra=dict(self.extra),
        )

    def _record(self, *call) -> None:
        self.calls.append(call)
        if self.fail:
            raise BackendError("could not save")

    def set_contact_photos(self, enabled: bool) -> dict:
        self.saved.append(enabled)
        self._record("contact_photos", enabled)
        self.photos = enabled
        return {"contact_photos": enabled}

    def set_otp_autocopy(self, enabled: bool) -> dict:
        self.saved.append(enabled)
        self._record("otp_autocopy", enabled)
        self.otp = enabled
        return {"otp_autocopy": enabled}

    def set_phone_battery_warning(self, enabled: bool) -> bool:
        self._record("battery", enabled)
        self.battery = enabled
        return enabled

    def set_proximity_lock(self, enabled: bool, grace: int) -> dict:
        self._record("proximity", enabled, grace)
        self.extra["proximity_lock_enabled"] = enabled
        return {"proximity_lock_enabled": enabled, "proximity_lock_grace_sec": grace}

    def set_auto_profile_reset(self, enabled: bool) -> bool:
        self._record("auto_reset", enabled)
        self.extra["profile_reset_auto"] = enabled
        return enabled

    def set_phone_audio_route(self, route: str) -> str:
        self._record("route", route)
        self.extra["phone_audio_route"] = route
        return route

    def set_call_history(self, enabled: bool, missed: bool) -> dict:
        self._record("call_history", enabled, missed)
        self.extra.update(call_history_enabled=enabled, missed_call_notifications=missed)
        return {"call_history_enabled": enabled, "missed_call_notifications": missed}

    def set_media_control(self, enabled: bool) -> dict:
        self._record("media", enabled)
        self.extra["media_control_enabled"] = enabled
        return {"media_control_enabled": enabled}

    def set_notification_policy(self, policy: str) -> str:
        self._record("policy", policy)
        self.policy = policy
        return policy

    def sync_contacts(self) -> int:
        self._record("sync")
        return 7

    def reconnect_phone(self) -> str:
        self._record("reconnect")
        return "started"

    def notification_open_map(self) -> list:
        return []

    def features(self) -> dict:
        self.feature_loads += 1
        if self.feature_items is None:
            raise BackendError("Unknown method GetFeatures")
        return {name: dict(info) for name, info in self.feature_items.items()}

    def set_feature(self, name: str, enabled: bool) -> str:
        self._record("feature", name, enabled)
        assert self.feature_items is not None
        info = self.feature_items[name]
        info["value"] = enabled
        info["restart_required"] = enabled != info["running"]
        return "restart-required" if info["restart_required"] else "active"


async def _until(pilot, predicate) -> None:
    for _attempt in range(60):
        if predicate():
            return
        await pilot.pause(0.05)
    assert predicate()


async def _mounted(pilot, app, kind):
    await _until(pilot, lambda: isinstance(app.screen, kind) and app.screen.is_mounted)
    await pilot.pause(0.05)
    return app.screen


async def _open(app, pilot, category: str = "phone") -> OptionsScreen:
    app.action_options(category)
    await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
    screen = app.screen
    assert isinstance(screen, OptionsScreen)
    await _until(pilot, lambda: screen.values is not None and screen.features["loaded"])
    return screen


def _app(backend, **options) -> BlueFerryApp:
    return BlueFerryApp(TuiState(backend), monitor_factory=lambda: None, **options)


def _plain(screen, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())


def test_the_categories_follow_the_qt_and_gtk_settings() -> None:
    assert [c.key for c in categories()] == [
        "phone", "notifications", "calls", "security", "about",
    ]
    # Every row named in a group exists, and every row is placed once.
    placed = [item for group in GROUPS for item in group.items]
    assert len(placed) == len(set(placed))
    assert {f"opt:{key}" for key in SAVED_SWITCHES} <= set(placed)
    assert {f"feature:{name}" for name in FEATURE_ROWS} <= set(placed)
    assert {group.category for group in GROUPS} == {c.key for c in categories()}


def test_options_screen_saves_the_contact_photo_opt_in() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("o")
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.values is not None)
            assert screen.query_one("#options-tabs", TabbedContent).active == "options-phone"
            checkbox = screen.query_one("#opt-contact_photos", Checkbox)
            assert checkbox.display is True
            assert checkbox.value is False

            checkbox.toggle()
            await _until(pilot, lambda: backend.saved == [True] and not screen.pending)
            assert checkbox.value is True
            assert screen.values["contact_photos"] is True

            await pilot.press("o")
            await _until(pilot, lambda: not isinstance(app.screen, OptionsScreen))

    asyncio.run(scenario())


def test_options_screen_saves_the_one_time_code_opt_in_under_security() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "security")
            assert screen.query_one("#options-tabs", TabbedContent).active == "options-security"
            checkbox = screen.query_one("#opt-otp_autocopy", Checkbox)
            assert checkbox.display is True
            assert checkbox.value is False

            checkbox.toggle()
            await _until(pilot, lambda: backend.saved == [True] and not screen.pending)
            assert checkbox.value is True
            assert screen.values["otp_autocopy"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [None, False, True])
def test_an_older_daemon_hides_what_it_lacks_and_names_the_local_env_variable(
    reported,
) -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(
            photos=reported, otp=reported, saved_opt_ins=False, extra={}, features=None,
        )
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            for key in ("contact_photos", "otp_autocopy", "proximity_lock_enabled",
                        "phone_battery_warning", "profile_reset_auto", "phone_audio"):
                assert screen.query_one(f"#opt-{key}", Checkbox).display is False
            for group in ("photos", "otp", "away-lock", "battery", "sound", "media"):
                assert screen.query_one(f"#options-group-{group}").display is False
            # No GetFeatures: the switch stays visible but disabled, naming
            # the variable to set in local.env instead.
            history = screen.query_one("#feature-notification_history", Checkbox)
            assert history.disabled is True
            assert "BLUEFERRY_NOTIFICATION_HISTORY" in _plain(
                screen, "#feature-notification_history-caption",
            )
            assert "local.env" in _plain(screen, "#feature-notification_history-caption")
            assert screen.query_one("#options-restart").display is False

    asyncio.run(scenario())


def test_a_failed_save_keeps_the_daemon_state_and_shows_the_error() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(fail=True)
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            checkbox = screen.query_one("#opt-contact_photos", Checkbox)
            checkbox.toggle()
            await _until(pilot, lambda: backend.saved == [True] and not screen.pending)
            assert checkbox.value is False
            assert "could not save" in _plain(screen, "#options-error")

    asyncio.run(scenario())


def test_away_lock_keeps_the_saved_grace_period() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "security")
            assert "90 seconds" in _plain(screen, "#opt-proximity_lock_enabled-caption")
            screen.query_one("#opt-proximity_lock_enabled", Checkbox).toggle()
            await _until(pilot, lambda: ("proximity", True, 90) in backend.calls)
            await _until(pilot, lambda: not screen.pending)
            assert screen.query_one("#opt-proximity_lock_enabled", Checkbox).value is True

    asyncio.run(scenario())


def test_battery_warning_shows_the_threshold_and_saves() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "calls")
            assert "15 %" in _plain(screen, "#opt-phone_battery_warning-caption")
            screen.query_one("#opt-phone_battery_warning", Checkbox).toggle()
            await _until(pilot, lambda: ("battery", True) in backend.calls)
            await _until(pilot, lambda: not screen.pending)
            assert screen.values["phone_battery_warning"] is True

    asyncio.run(scenario())


def test_dependent_switches_wait_for_their_parent() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "calls")
            assert screen.query_one("#opt-missed_call_notifications", Checkbox).disabled
            assert screen.query_one("#opt-media_mpris_enabled", Checkbox).disabled
            screen.query_one("#opt-call_history_enabled", Checkbox).toggle()
            # The missed-call choice is sent along, as in the GTK client.
            await _until(pilot, lambda: ("call_history", True, True) in backend.calls)
            await _until(pilot, lambda: not screen.pending)
            assert not screen.query_one("#opt-missed_call_notifications", Checkbox).disabled

    asyncio.run(scenario())


def test_sound_route_and_automatic_reset_are_options_too() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "calls")
            route = screen.query_one("#opt-phone_audio", Checkbox)
            assert route.display is True and route.value is False
            route.toggle()
            await _until(pilot, lambda: ("route", "pc") in backend.calls)
            await _until(pilot, lambda: not screen.pending)
            assert route.value is True

            screen.query_one("#opt-profile_reset_auto", Checkbox).toggle()
            await _until(pilot, lambda: ("auto_reset", True) in backend.calls)

    asyncio.run(scenario())


def test_popup_policy_saves_through_the_select() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(policy="all")
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            select = screen.query_one("#options-policy", Select)
            await pilot.pause(0.2)
            # Opening and rendering never writes the policy back.
            assert select.value == "all"
            assert not [call for call in backend.calls if call[0] == "policy"]
            select.value = "none"
            await _until(pilot, lambda: ("policy", "none") in backend.calls)
            await _until(pilot, lambda: "policy" not in screen.pending)
            assert screen.values["notification_policy"] == "none"
            # Contacts-only has no meaning without popups.
            assert screen.query_one("#opt-contacts_only_notifications", Checkbox).disabled

    asyncio.run(scenario())


def test_a_feature_switch_asks_for_a_restart_and_restart_service_reloads() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(features={
            "show_notification_content": _feature(True),
            "mark_read_on_dismiss": _feature(True),
            "notification_history": _feature(False),
        })
        restarts: list[str] = []
        app = _app(backend, restart=lambda: restarts.append("restart"))
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            assert screen.query_one("#options-restart").display is False
            history = screen.query_one("#feature-notification_history", Checkbox)
            assert history.disabled is False and history.value is False

            history.toggle()
            await _until(pilot, lambda: ("feature", "notification_history", True) in backend.calls)
            await _until(pilot, lambda: not screen.saving_features)
            await _until(pilot, lambda: screen.query_one("#options-restart").display is True)
            assert history.value is True
            assert "Restart" in _plain(screen, "#options-restart-notice")
            assert "Applies after the service restarts." in _plain(
                screen, "#feature-notification_history-caption",
            )

            loads = backend.feature_loads
            backend.feature_items["notification_history"]["running"] = True
            backend.feature_items["notification_history"]["restart_required"] = False
            await pilot.click("#options-restart-button")
            await _until(pilot, lambda: restarts == ["restart"])
            # The switches reload after the restart delay, through a timer.
            await _until(pilot, lambda: backend.feature_loads > loads)
            await _until(pilot, lambda: screen.query_one("#options-restart").display is False)
            assert not screen.restarting

    asyncio.run(scenario())


def test_a_feature_pinned_by_the_environment_cannot_change() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(features={
            "show_notification_content": _feature(
                True, source="environment", variable="BLUEFERRY_SHOW_NOTIFICATION_CONTENT",
            ),
        })
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            pinned = screen.query_one("#feature-show_notification_content", Checkbox)
            assert pinned.disabled is True and pinned.value is True
            assert "service's environment" in _plain(
                screen, "#feature-show_notification_content-caption",
            )

    asyncio.run(scenario())


def test_a_failed_feature_save_puts_the_switch_back() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(features={"mark_read_on_dismiss": _feature(True)})
        backend.fail = True
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            switch = screen.query_one("#feature-mark_read_on_dismiss", Checkbox)
            switch.toggle()
            await _until(pilot, lambda: backend.calls == [
                ("feature", "mark_read_on_dismiss", False),
            ] and not screen.saving_features)
            assert switch.value is True
            assert "could not save" in _plain(screen, "#options-error")

    asyncio.run(scenario())


def test_a_failed_restart_is_reported_even_after_options_closed() -> None:
    async def scenario() -> None:
        release = threading.Event()

        def restart() -> None:
            release.wait(5)
            raise RuntimeError("systemctl failed")

        backend = _OptionsBackend(features={})
        app = _app(backend, restart=restart)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "about")
            assert "Running" in _plain(screen, "#options-service")
            await pilot.click("#options-service-restart")
            await _until(pilot, lambda: screen.restarting)
            assert screen.query_one("#options-service-restart", Button).disabled
            screen.dismiss(None)
            await _until(pilot, lambda: not isinstance(app.screen, OptionsScreen))
            release.set()
            await _until(pilot, lambda: any(
                "systemctl failed" in str(n.message) for n in app._notifications
            ))

    asyncio.run(scenario())


def test_reconnect_and_click_rules_are_reachable_from_their_category() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            screen = await _open(app, pilot)
            await _until(pilot, lambda: screen.query_one("#options-reconnect-row").display)
            await pilot.click("#options-reconnect")
            await _until(pilot, lambda: ("reconnect",) in backend.calls)

            screen.query_one("#options-tabs", TabbedContent).active = "options-notifications"
            await pilot.pause(0.1)
            await pilot.click("#options-click-rules")
            await _until(pilot, lambda: isinstance(app.screen, NotificationRulesScreen))

    asyncio.run(scenario())


def test_sync_contacts_reports_the_count() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            screen.query_one("#options-sync-contacts", Button).press()
            await _until(pilot, lambda: any(
                "Synced 7" in str(n.message) for n in app._notifications
            ))
            assert not screen.query_one("#options-sync-contacts", Button).disabled

    asyncio.run(scenario())


def test_the_app_forwards_a_new_status_to_open_options() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(extra={})
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "security")
            assert screen.query_one("#opt-proximity_lock_enabled", Checkbox).display is False
            backend.extra = dict(_FULL_EXTRA, proximity_lock_enabled=True)
            app.action_refresh()
            await _until(
                pilot, lambda: screen.query_one("#opt-proximity_lock_enabled", Checkbox).display,
            )
            assert screen.query_one("#opt-proximity_lock_enabled", Checkbox).value is True

    asyncio.run(scenario())


def test_the_command_palette_opens_a_category_and_the_diagnostics() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        report = DoctorReport(True, False, "all good")
        app = _app(backend, doctor=lambda: report)
        async with app.run_test(size=(120, 40)) as pilot:
            commands = {command.title: command for command in app.get_system_commands(app.screen)}
            assert "Settings: Security" in commands and "Run Diagnostics" in commands
            commands["Settings: Calls & Audio"].callback()
            await _mounted(pilot, app, OptionsScreen)
            tabs = app.screen.query_one("#options-tabs", TabbedContent)
            assert tabs.active == "options-calls"
            # From within Options the palette switches the tab.
            commands["Settings: About"].callback()
            await _until(pilot, lambda: tabs.active == "options-about")
            commands["Run Diagnostics"].callback()
            await _mounted(pilot, app, DoctorScreen)

    asyncio.run(scenario())


def test_diagnostics_show_the_report_as_plain_text_and_copy_it() -> None:
    async def scenario() -> None:
        text = "Configured iPhone: [b]Robin[/b]\nWARNING: notification link down"
        app = _app(_OptionsBackend(), doctor=lambda: DoctorReport(True, True, text))
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot)
            await pilot.click("#options-doctor")
            await _until(pilot, lambda: isinstance(app.screen, DoctorScreen))
            doctor = app.screen
            await _until(pilot, lambda: doctor.state.get("ran") is True)
            assert "warnings" in str(doctor.query_one("#doctor-summary", Static).render())
            report = doctor.query_one("#doctor-report", TextArea)
            assert report.read_only and report.text == text
            await pilot.click("#doctor-copy")
            assert app.clipboard == text
            await pilot.press("escape")
            await _until(pilot, lambda: app.screen is screen)

    asyncio.run(scenario())


def test_closing_diagnostics_while_it_runs_ends_the_check(monkeypatch) -> None:
    async def scenario() -> None:
        release = threading.Event()
        terminated: list[bool] = []

        class _Runner:
            def run(self) -> DoctorReport:
                release.wait(5)
                return DoctorReport(False, False, "late")

            def stop(self) -> None:
                terminated.append(True)
                release.set()

        monkeypatch.setattr(tui_doctor, "DoctorRunner", _Runner)
        app = _app(_OptionsBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_diagnostics()
            doctor = await _mounted(pilot, app, DoctorScreen)
            assert doctor.query_one("#doctor-run", Button).disabled
            assert "Running checks" in str(doctor.query_one("#doctor-summary", Static).render())
            await pilot.press("escape")
            await _until(pilot, lambda: terminated)
            await pilot.pause(0.1)
            # The late answer is dropped with the screen.
            assert doctor.state.get("ran") is False

    asyncio.run(scenario())


def test_a_failed_diagnostics_run_says_so() -> None:
    async def scenario() -> None:
        def broken() -> DoctorReport:
            raise OSError("no python")

        app = _app(_OptionsBackend(), doctor=broken)
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_diagnostics()
            doctor = await _mounted(pilot, app, DoctorScreen)
            await _until(pilot, lambda: doctor.state.get("ran") is True)
            assert "failed" in str(doctor.query_one("#doctor-summary", Static).render())
            assert "no python" in doctor.query_one("#doctor-report", TextArea).text

    asyncio.run(scenario())


def test_a_toggle_survives_a_status_render_before_its_handler() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "calls")
            checkbox = screen.query_one("#opt-phone_battery_warning", Checkbox)
            checkbox.toggle()
            # The 15 s poll or StatusChanged renders the daemon's (old) value
            # before the Changed message is handled.
            screen.status_changed(backend.status())
            await _until(pilot, lambda: ("battery", True) in backend.calls)
            await _until(pilot, lambda: not screen.pending)
            assert checkbox.value is True

    asyncio.run(scenario())


def test_a_policy_choice_survives_a_status_render_and_stays_shown_while_saving() -> None:
    async def scenario() -> None:
        release = threading.Event()
        backend = _OptionsBackend(policy="all")
        original = backend.set_notification_policy

        def slow(policy: str) -> str:
            release.wait(5)
            return original(policy)

        backend.set_notification_policy = slow  # type: ignore[method-assign]
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            select = screen.query_one("#options-policy", Select)
            select.value = "none"
            screen.status_changed(backend.status())
            await _until(pilot, lambda: screen.pending.get("policy") == "none")
            # A render while saving keeps the user's choice.
            screen.status_changed(backend.status())
            await pilot.pause(0.05)
            assert select.value == "none"
            release.set()
            await _until(pilot, lambda: ("policy", "none") in backend.calls)
            await _until(pilot, lambda: "policy" not in screen.pending)
            assert select.value == "none"

    asyncio.run(scenario())


def test_the_palette_opens_options_while_search_has_the_focus() -> None:
    async def scenario() -> None:
        app = _app(_OptionsBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#thread-search").focus()
            await pilot.pause(0.05)
            # Typing "o" in search stays text.
            await pilot.press("o")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, OptionsScreen)
            commands = {command.title: command for command in app.get_system_commands(app.screen)}
            commands["Settings: Security"].callback()
            screen = await _mounted(pilot, app, OptionsScreen)
            assert screen.query_one("#options-tabs", TabbedContent).active == "options-security"

    asyncio.run(scenario())


def test_reopening_options_during_a_restart_cannot_start_a_second_one() -> None:
    async def scenario() -> None:
        release = threading.Event()
        restarts: list[str] = []

        def restart() -> None:
            restarts.append("restart")
            release.wait(5)

        app = _app(_OptionsBackend(features={}), restart=restart)
        async with app.run_test(size=(120, 40)) as pilot:
            first = await _open(app, pilot, "about")
            first.restart_service()
            await _until(pilot, lambda: restarts == ["restart"])
            first.dismiss(None)
            await _until(pilot, lambda: not isinstance(app.screen, OptionsScreen))
            second = await _open(app, pilot, "about")
            assert second.restarting
            assert second.query_one("#options-service-restart", Button).disabled
            second.restart_service()
            release.set()
            await _until(pilot, lambda: not second.restarting)
            await pilot.pause(0.1)
            assert restarts == ["restart"]
            assert not second.query_one("#options-service-restart", Button).disabled

    asyncio.run(scenario())


def test_the_policy_select_mounts_once_without_an_initial_changed(monkeypatch) -> None:
    from textual.widgets import Select as TextualSelect

    from blueferry.tui_options import PolicySelect

    mounts: list[object] = []
    original = TextualSelect._on_mount

    def counting(self, event) -> None:
        mounts.append(self)
        original(self, event)

    monkeypatch.setattr(TextualSelect, "_on_mount", counting)

    async def scenario() -> None:
        changes: list[object] = []
        app = _app(_OptionsBackend(policy="all"))
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _open(app, pilot, "notifications")
            select = screen.query_one("#options-policy", PolicySelect)
            assert mounts.count(select) == 1
            screen.policy_changed = lambda event: changes.append(event.value)  # type: ignore[method-assign]
            await pilot.pause(0.1)
            assert changes == []

    asyncio.run(scenario())


def test_a_restart_ending_while_options_is_being_pushed_does_not_crash() -> None:
    async def scenario() -> None:
        release = threading.Event()
        app = _app(_OptionsBackend(features={}), restart=lambda: release.wait(5))
        async with app.run_test(size=(120, 40)) as pilot:
            first = await _open(app, pilot, "about")
            first.restart_service()
            await _until(pilot, lambda: first.restarting)
            first.dismiss(None)
            await _until(pilot, lambda: not isinstance(app.screen, OptionsScreen))
            app._push_options("about")
            release.set()
            second = await _mounted(pilot, app, OptionsScreen)
            await _until(pilot, lambda: not second.restarting)
            await pilot.pause(0.1)
            assert app.is_running
            assert not second.query_one("#options-service-restart", Button).disabled

    asyncio.run(scenario())


def test_a_status_forwarded_while_options_is_being_pushed_does_not_crash() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = _app(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app._push_options("phone")
            # A new status arrives before the screen has composed.
            backend.battery = True
            app._schedule_snapshot(app.state.fetch_snapshot())
            screen = await _mounted(pilot, app, OptionsScreen)
            await _until(pilot, lambda: screen.values is not None)
            await pilot.pause(0.1)
            assert app.is_running

    asyncio.run(scenario())
