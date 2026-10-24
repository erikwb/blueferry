"""`blueferry reconnect` with a fake backend; never pages a phone."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli_reconnect
from blueferry.cli import app
from blueferry.client import BackendError
from blueferry.models import BackendStatus
from blueferry.reconnect_view import UNREACHABLE_TEXT, reconnect_view, result_text


class _Backend:
    def __init__(self, result, states):
        self.result = result
        self.states = list(states)
        self.calls = 0

    def reconnect_phone(self) -> str:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def status(self) -> BackendStatus:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return BackendStatus.from_dict({
            "daemon": True, "phone_reconnect_state": state,
            "phone_reconnect_paused": False, "phone_reconnect_next_in_sec": 30,
        })


def _run(monkeypatch, backend, *args):
    monkeypatch.setattr(cli_reconnect, "_client", lambda: backend)
    monkeypatch.setattr(cli_reconnect, "_sleep", lambda _seconds: None)
    return CliRunner().invoke(app, ["reconnect", *args])


def test_reconnect_waits_until_connected(monkeypatch) -> None:
    backend = _Backend("started", ["connecting", "waiting", "connected"])
    result = _run(monkeypatch, backend)
    assert result.exit_code == 0, result.output
    assert "connected" in result.output
    assert backend.calls == 1


def test_reconnect_explains_an_unreachable_phone(monkeypatch) -> None:
    result = _run(monkeypatch, _Backend("started", ["connecting", "unreachable"]))
    assert result.exit_code == 1
    assert UNREACHABLE_TEXT in result.output
    result = _run(monkeypatch, _Backend("unreachable", ["unreachable"]))
    assert result.exit_code == 1
    assert "Bluetooth switched on on the iPhone" in result.output
    result = _run(monkeypatch, _Backend("bluez-kernel", ["waiting"]))
    assert result.exit_code == 1
    assert "Restart the computer" in result.output


def test_reconnect_in_progress_and_no_wait(monkeypatch) -> None:
    result = _run(monkeypatch, _Backend("in-progress", ["connecting"]))
    assert result.exit_code == 0
    assert "already running" in result.output
    result = _run(monkeypatch, _Backend("started", ["connecting"]), "--no-wait")
    assert result.exit_code == 0
    assert "Reconnecting" in result.output


@pytest.mark.parametrize("state", ["connecting", "waiting"])
def test_reconnect_timeout_is_a_failure(monkeypatch, state) -> None:
    result = _run(monkeypatch, _Backend("started", [state]))
    assert result.exit_code == 1
    assert "Timed out" in result.output


@pytest.mark.parametrize("state", ["waiting", "connected"])
def test_reconnect_reports_bluez_hang_during_wait_before_using_cached_link(monkeypatch, state):
    class Hung(_Backend):
        def status(self):
            return BackendStatus.from_dict({
                "daemon": True, "phone_reconnect_state": state,
                "bluez_unresponsive": True, "bluez_unresponsive_reason": "kernel",
            })

    sleeps = []
    monkeypatch.setattr(cli_reconnect, "_client", lambda: Hung("started", []))
    monkeypatch.setattr(cli_reconnect, "_sleep", sleeps.append)
    result = CliRunner().invoke(app, ["reconnect"])
    assert result.exit_code == 1
    assert "stuck in the kernel" in result.output
    assert sleeps == [1.0]


def test_reconnect_status_errors_cannot_end_as_success(monkeypatch) -> None:
    class Unavailable(_Backend):
        def status(self):
            raise BackendError("backend unavailable")

    result = _run(monkeypatch, Unavailable("started", []))
    assert result.exit_code == 1
    assert "Timed out" in result.output


def test_reconnect_reports_backend_errors(monkeypatch) -> None:
    result = _run(monkeypatch, _Backend(BackendError("no iPhone is being supervised"), ["waiting"]))
    assert result.exit_code == 2
    assert "no iPhone" in result.output


def test_reconnect_view_texts() -> None:
    assert reconnect_view({}).available is False
    assert reconnect_view({"daemon": True, "phone_reconnect_state": "connected"}).offered is False
    paused = reconnect_view({
        "daemon": True, "phone_reconnect_state": "unreachable",
        "phone_reconnect_paused": True, "phone_reconnect_next_in_sec": 540,
    })
    assert paused.offered and "9 min" in paused.hint
    soon = reconnect_view({
        "daemon": True, "phone_reconnect_state": "unreachable",
        "phone_reconnect_next_in_sec": 20,
    })
    assert "20 s" in soon.hint
    assert result_text("bogus") == UNREACHABLE_TEXT
    assert paused.attention and soon.attention
    waiting = reconnect_view({"daemon": True, "phone_reconnect_state": "waiting"})
    assert waiting.offered and not waiting.attention


def test_reconnect_error_texts_follow_the_dbus_error() -> None:
    from blueferry.reconnect_view import UNSUPPORTED_TEXT, reconnect_error_text

    assert reconnect_error_text("org.freedesktop.DBus.Error.UnknownMethod") == UNSUPPORTED_TEXT
    assert "failed" in reconnect_error_text("")


def test_rate_limits_read_as_a_request_to_wait() -> None:
    import dbus

    from blueferry.client import _dbus_message
    from blueferry.reconnect_view import RATE_LIMITED_TEXT, reconnect_error_text

    assert reconnect_error_text("io.weirdware.BlueFerry.Error.RateLimited") == RATE_LIMITED_TEXT
    assert "too many" not in RATE_LIMITED_TEXT.casefold()
    error = dbus.exceptions.DBusException(
        "too many requests", name="io.weirdware.BlueFerry.Error.RateLimited",
    )
    assert _dbus_message(error) == RATE_LIMITED_TEXT
    other = dbus.exceptions.DBusException("no phone", name="io.weirdware.BlueFerry.Error.NotReady")
    assert _dbus_message(other) == "no phone"


def test_reconnect_during_a_call_says_so(monkeypatch) -> None:
    result = _run(monkeypatch, _Backend("call-active", ["connected"]))
    assert result.exit_code == 1
    assert "call is active" in result.output
    assert result_text("call-active") != result_text("connected")


def test_reconnect_saves_the_automatic_reset_choice(monkeypatch) -> None:
    class Saving(_Backend):
        def __init__(self, error=None):
            super().__init__("started", ["connected"])
            self.saved: list[bool] = []
            self.error = error

        def set_auto_profile_reset(self, enabled: bool) -> bool:
            if self.error is not None:
                raise self.error
            self.saved.append(enabled)
            return enabled

    backend = Saving()
    result = _run(monkeypatch, backend, "--no-auto-reset")
    assert result.exit_code == 0, result.output
    assert "Automatic profile reset off." in result.output
    assert backend.saved == [False] and backend.calls == 0
    result = _run(monkeypatch, backend, "--auto-reset")
    assert "Automatic profile reset on." in result.output
    failing = Saving(BackendError("Unknown method"))
    result = _run(monkeypatch, failing, "--no-auto-reset")
    assert result.exit_code == 2
    assert "Could not save the setting" in result.output
