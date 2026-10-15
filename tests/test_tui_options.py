"""The terminal client's opt-in settings screen."""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Checkbox, Static

from blueferry.client import BackendError
from blueferry.models import BackendStatus
from blueferry.tui import BlueFerryApp, TuiState
from blueferry.tui_options import OptionsScreen
from tests.test_tui import _Backend


class _OptionsBackend(_Backend):
    def __init__(
        self, *, photos: bool | None = False, otp: bool | None = False,
        saved_opt_ins: bool = True, fail: bool = False,
    ) -> None:
        super().__init__()
        self.photos = photos
        self.otp = otp
        self.fail = fail
        self.saved_opt_ins = saved_opt_ins
        self.saved: list[bool] = []

    def status(self) -> BackendStatus:  # type: ignore[override]
        return BackendStatus(
            daemon=True, map=True, pbap=True, ancs=True,
            contact_photos=self.photos, otp_autocopy=self.otp,
            saved_opt_ins=self.saved_opt_ins,
        )

    def set_contact_photos(self, enabled: bool) -> dict:
        self.saved.append(enabled)
        if self.fail:
            raise BackendError("could not save")
        self.photos = enabled
        return {"contact_photos": enabled}

    def set_otp_autocopy(self, enabled: bool) -> dict:
        self.saved.append(enabled)
        if self.fail:
            raise BackendError("could not save")
        self.otp = enabled
        return {"otp_autocopy": enabled}


async def _until(pilot, predicate) -> None:
    for _attempt in range(60):
        if predicate():
            return
        await pilot.pause(0.05)
    assert predicate()


def test_options_screen_saves_the_contact_photo_opt_in() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("o")
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.values is not None)
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


def test_options_screen_saves_the_one_time_code_opt_in() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend()
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("o")
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.values is not None)
            checkbox = screen.query_one("#opt-otp_autocopy", Checkbox)
            assert checkbox.display is True
            assert checkbox.value is False

            checkbox.toggle()
            await _until(pilot, lambda: backend.saved == [True] and not screen.pending)
            assert checkbox.value is True
            assert screen.values["otp_autocopy"] is True

            await pilot.press("o")
            await _until(pilot, lambda: not isinstance(app.screen, OptionsScreen))

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [None, False, True])
def test_options_screen_hides_settings_an_older_daemon_lacks(reported) -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(photos=reported, otp=reported, saved_opt_ins=False)
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            app.action_options()
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.values is not None)
            assert screen.query_one("#opt-contact_photos", Checkbox).display is False
            assert screen.query_one("#opt-otp_autocopy", Checkbox).display is False
            note = screen.query_one("#options-note", Static)
            assert "no optional features" in note.render().plain

    asyncio.run(scenario())


def test_a_failed_save_keeps_the_daemon_state_and_shows_the_error() -> None:
    async def scenario() -> None:
        backend = _OptionsBackend(fail=True)
        app = BlueFerryApp(TuiState(backend), monitor_factory=lambda: None)
        async with app.run_test(size=(120, 36)) as pilot:
            app.action_options()
            await _until(pilot, lambda: isinstance(app.screen, OptionsScreen))
            screen = app.screen
            await _until(pilot, lambda: screen.values is not None)
            checkbox = screen.query_one("#opt-contact_photos", Checkbox)
            checkbox.toggle()
            await _until(pilot, lambda: backend.saved == [True] and not screen.pending)
            assert checkbox.value is False
            assert "could not save" in screen.query_one("#options-error", Static).render().plain

    asyncio.run(scenario())
