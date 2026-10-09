"""Adapter Class-of-Device supervision without Bluetooth or systemd."""

import pytest

from blueferry import adapter_class_supervisor
from blueferry.adapter_class_supervisor import AdapterClassSupervisor


def _supervisor(calls, state, scheduled):
    return AdapterClassSupervisor(
        "hci7",
        read_class=lambda adapter: (
            calls.append(("read", adapter)),
            state["class"],
        )[-1],
        matches=lambda value: value == 0x408,
        repair=lambda adapter: (
            calls.append(("repair", adapter)),
            state.__setitem__("class", 0x408),
            True,
        )[-1],
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda timer_id: calls.append(("cancel", timer_id)),
    )


def test_start_repairs_a_drifted_adapter_class() -> None:
    calls = []
    state = {"class": 0x104}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)

    supervisor.start()

    assert calls == [("read", "hci7"), ("repair", "hci7")]
    assert state["class"] == 0x408
    assert scheduled[0][0] == adapter_class_supervisor.RECONCILE_SECONDS


def test_matching_adapter_class_is_left_alone() -> None:
    calls = []
    state = {"class": 0x408}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)

    supervisor.start()

    assert calls == [("read", "hci7")]


def test_periodic_reconciliation_repairs_later_drift() -> None:
    calls = []
    state = {"class": 0x408}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()

    state["class"] = 0x104
    assert scheduled[0][1]() is True

    assert calls[-2:] == [("read", "hci7"), ("repair", "hci7")]


def test_unknown_adapter_state_waits_without_invoking_helper() -> None:
    calls = []
    state = {"class": None}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)

    supervisor.start()

    assert calls == [("read", "hci7")]


def test_read_failure_does_not_stop_periodic_reconciliation() -> None:
    scheduled = []

    def fail(_adapter):
        raise RuntimeError("gone")

    supervisor = AdapterClassSupervisor(
        "hci7",
        read_class=fail,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer: None,
    )

    supervisor.start()

    assert scheduled[0][1]() is True


def test_bluez_restart_poke_rechecks_immediately() -> None:
    calls = []
    state = {"class": 0x408}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()
    state["class"] = 0x104

    supervisor.poke()

    assert calls[-2:] == [("read", "hci7"), ("repair", "hci7")]


def test_stop_cancels_reconciliation() -> None:
    calls = []
    state = {"class": 0x408}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()

    supervisor.stop()

    assert calls[-1] == ("cancel", 7)
    assert scheduled[0][1]() is False


def _failing_supervisor(attempts, scheduled, now, error=None, state=None):
    from blueferry.bluez_setup import CodAuthorizationRefused

    state = state if state is not None else {"class": 0x104}

    def repair(adapter):
        attempts.append(adapter)
        if error == "refused":
            raise CodAuthorizationRefused("add a sudoers rule")
        if error == "raise":
            raise RuntimeError("polkit agent missing")
        return False

    return AdapterClassSupervisor(
        "hci7",
        read_class=lambda _adapter: state["class"],
        matches=lambda value: value == 0x408,
        repair=repair,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer_id: None,
        clock=lambda: now[0],
    )


def _run_minutes(scheduled, now, minutes):
    for _minute in range(minutes):
        now[0] += 60
        assert scheduled[0][1]() is True


def test_authorization_refusal_backs_off_instead_of_retrying_every_minute() -> None:
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error="refused")

    supervisor.start()
    _run_minutes(scheduled, now, 14)
    assert attempts == ["hci7"]

    # The pause is not sticky: a sudoers rule added later takes effect
    # without a Bluetooth restart, at 15, then 45, then 105 minutes.
    _run_minutes(scheduled, now, 1)
    assert len(attempts) == 2
    _run_minutes(scheduled, now, 30)
    assert len(attempts) == 3
    _run_minutes(scheduled, now, 60)
    assert len(attempts) == 4


def test_refusal_backoff_is_capped_so_a_day_logs_only_a_handful_of_attempts() -> None:
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error="refused")

    supervisor.start()
    _run_minutes(scheduled, now, 24 * 60)

    # 0, 15 min, 45 min, 1h45, 3h45, 7h45 (cap 6h), 13h45, 19h45.
    assert len(attempts) == 8
    assert adapter_class_supervisor.MAX_REPAIR_BACKOFF_SECONDS == 6 * 60 * 60


@pytest.mark.parametrize("error", [None, "raise"])
def test_unrecognized_failures_also_back_off(error) -> None:
    # Unknown sudo wording, a missing polkit agent, or btmgmt failing all
    # land here; none may turn into one authentication event per minute.
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error=error)

    supervisor.start()
    _run_minutes(scheduled, now, 60)

    # 0, 1, 3, 7, 15, 31 minutes.
    assert len(attempts) == 6


def test_bluez_restart_poke_clears_the_backoff() -> None:
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error="refused")
    supervisor.start()
    _run_minutes(scheduled, now, 5)
    assert attempts == ["hci7"]

    supervisor.poke()

    assert attempts == ["hci7", "hci7"]


def test_a_matching_class_clears_the_backoff() -> None:
    attempts, scheduled, now = [], [], [0.0]
    state = {"class": 0x104}
    supervisor = _failing_supervisor(
        attempts, scheduled, now, error="refused", state=state,
    )
    supervisor.start()

    # The admin ran the helper by hand; a later drift is repaired at once.
    state["class"] = 0x408
    _run_minutes(scheduled, now, 1)
    state["class"] = 0x104
    _run_minutes(scheduled, now, 1)

    assert attempts == ["hci7", "hci7"]


def test_supervisor_restart_does_not_reuse_an_old_authorization_backoff() -> None:
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error="refused")
    supervisor.start()
    _run_minutes(scheduled, now, 5)
    assert attempts == ["hci7"]
    supervisor.stop()
    supervisor.start()
    assert attempts == ["hci7", "hci7"]
