"""Pure transfer-state tests; no BlueZ or OBEX connection is opened."""
from __future__ import annotations

import dbus
import pytest

from blueferry.obex.transfer import TransferFailed, wait_for_transfer


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_requires_terminal_completion() -> None:
    statuses = iter(["queued", "active", "complete"])
    clock = _Clock()

    assert wait_for_transfer(
        "/transfer/1", timeout_s=5, get_status=lambda: next(statuses),
        monotonic=clock, sleep=clock.sleep,
    ) == "complete"


def test_nonterminal_status_at_deadline_is_not_success() -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match="last status: active"):
        wait_for_transfer(
            "/transfer/2", timeout_s=0.2, get_status=lambda: "active",
            monotonic=clock, sleep=clock.sleep,
        )


def test_transfer_timeout_restarts_when_progress_advances() -> None:
    clock = _Clock()
    polls = 0

    def status() -> str:
        nonlocal polls
        polls += 1
        return "complete" if polls == 8 else "active"

    assert wait_for_transfer(
        "/transfer/long",
        timeout_s=0.2,
        get_status=status,
        get_progress=lambda: int(clock.now / 0.2),
        monotonic=clock,
        sleep=clock.sleep,
    ) == "complete"
    assert clock.now > 0.2


def test_progress_regression_does_not_restart_inactivity_timeout() -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match=r"timed out after 0\.2s"):
        wait_for_transfer(
            "/transfer/regressing",
            timeout_s=0.2,
            get_status=lambda: "active",
            get_progress=lambda: 10 if clock.now < 0.1 else 9,
            monotonic=clock,
            sleep=clock.sleep,
        )


def test_progress_cannot_extend_overall_timeout() -> None:
    clock = _Clock()

    with pytest.raises(TimeoutError, match=r"0\.5s overall limit"):
        wait_for_transfer(
            "/transfer/slow-loris",
            timeout_s=0.2,
            overall_timeout_s=0.5,
            get_status=lambda: "active",
            get_progress=lambda: int(clock.now * 100),
            monotonic=clock,
            sleep=clock.sleep,
        )


def test_explicit_transfer_error_fails() -> None:
    with pytest.raises(TransferFailed):
        wait_for_transfer(
            "/transfer/3", initial_status="error", timeout_s=1,
        )


def test_only_object_disappearance_is_accepted() -> None:
    def gone():
        raise dbus.exceptions.DBusException(
            "gone", name="org.freedesktop.DBus.Error.UnknownObject",
        )

    assert wait_for_transfer(
        "/transfer/4", timeout_s=1, get_status=gone,
    ) == "gone"


def test_unrelated_dbus_failure_is_not_treated_as_completion() -> None:
    def disconnected():
        raise dbus.exceptions.DBusException(
            "lost", name="org.freedesktop.DBus.Error.Disconnected",
        )

    with pytest.raises(RuntimeError, match="Disconnected"):
        wait_for_transfer(
            "/transfer/5", timeout_s=1, get_status=disconnected,
        )
