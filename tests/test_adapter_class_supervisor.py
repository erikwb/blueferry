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


def _failing_supervisor(
    attempts, scheduled, now, error=None, state=None, uses_sudo=True,
):
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
        uses_sudo=lambda: uses_sudo,
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
def test_unrecognized_sudo_failures_back_off_but_only_for_minutes(error) -> None:
    # Unknown sudo wording or btmgmt failing without systemd must not turn
    # into one authentication event per minute, yet a cause that clears is
    # still repaired within a few minutes.
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(attempts, scheduled, now, error=error)

    supervisor.start()
    _run_minutes(scheduled, now, 60)

    # 0, 1, 3, 7, then every 5 minutes: 12, 17, ..., 57.
    assert len(attempts) == 14
    assert adapter_class_supervisor.MAX_FAILED_REPAIR_BACKOFF_SECONDS == 5 * 60


@pytest.mark.parametrize("error", [None, "raise"])
def test_systemd_failures_are_retried_every_minute_as_before(error) -> None:
    # A missing polkit agent or a failing unit can clear on its own, and
    # until the repair lands iOS refuses Classic MAP/PBAP: no backoff here.
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(
        attempts, scheduled, now, error=error, uses_sudo=False,
    )

    supervisor.start()
    _run_minutes(scheduled, now, 10 * 60)

    assert len(attempts) == 1 + 10 * 60


def test_systemd_failure_is_not_repeated_by_a_recheck_within_the_minute() -> None:
    # A completed asynchronous class read calls recheck(); on systemd it must
    # not turn one failing repair per minute into one per reply.
    attempts, scheduled, now = [], [], [0.0]
    supervisor = _failing_supervisor(
        attempts, scheduled, now, error=None, uses_sudo=False,
    )

    supervisor.start()
    now[0] = 20.0
    supervisor.recheck()
    assert attempts == ["hci7"]

    _run_minutes(scheduled, now, 1)
    assert attempts == ["hci7", "hci7"]


def test_systemd_repair_lands_one_minute_after_a_long_failure_clears() -> None:
    attempts, scheduled, now = [], [], [0.0]
    outcome = {"error": "raise"}
    state = {"class": 0x104}

    def repair(adapter):
        attempts.append(adapter)
        if outcome["error"]:
            raise RuntimeError("unit failed")
        state["class"] = 0x408
        return True

    supervisor = AdapterClassSupervisor(
        "hci7",
        read_class=lambda _adapter: state["class"],
        matches=lambda value: value == 0x408,
        repair=repair,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda _timer_id: None,
        clock=lambda: now[0],
        uses_sudo=lambda: False,
    )
    supervisor.start()
    _run_minutes(scheduled, now, 10 * 60)

    outcome["error"] = None
    _run_minutes(scheduled, now, 1)

    assert state["class"] == 0x408


def test_service_manager_decides_whether_failures_back_off(monkeypatch) -> None:
    from blueferry import service_manager

    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.SYSTEMD)
    assert adapter_class_supervisor._repairs_through_sudo() is False
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    assert adapter_class_supervisor._repairs_through_sudo() is True


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


def test_async_class_reply_is_consumed_without_refunding_repair_backoff():
    from types import SimpleNamespace

    from blueferry.bluez_reads import BluezReadCache

    now = 0.0
    replies, repairs = [], []
    cache = BluezReadCache(lambda: SimpleNamespace(
        call_async=lambda *args, **_kwargs: replies.append(args[-2]),
    ), lambda: now)
    supervisor = AdapterClassSupervisor(
        "hci7",
        read_class=lambda adapter: cache.property(
            f"/org/bluez/{adapter}", "org.bluez.Adapter1", "Class",
            on_ready=supervisor.recheck,
        ),
        matches=lambda value: value == 0x408,
        repair=lambda adapter: repairs.append(adapter) or False,
        schedule=lambda _delay, _callback: 7, cancel=lambda _source: None,
        clock=lambda: now,
    )
    supervisor.start()
    assert not repairs
    replies[-1](0x104)
    assert repairs == ["hci7"]
    now = 20
    supervisor.recheck()
    replies[-1](0x104)
    assert repairs == ["hci7"]  # Async completion did not clear the 60s backoff.
    now = 60
    supervisor.recheck()
    replies[-1](0x104)
    assert repairs == ["hci7", "hci7"]
    now = 200
    supervisor.recheck()
    supervisor.stop()
    replies[-1](0x104)
    assert len(repairs) == 2  # A reply after shutdown cannot run the helper.


class _Timers:
    """Fake GLib timers: ids, cancellation and firing by delay."""

    def __init__(self):
        self.pending = {}
        self.cancelled = []
        self._next = 1

    def schedule(self, delay, callback):
        timer_id = self._next
        self._next += 1
        self.pending[timer_id] = (delay, callback)
        return timer_id

    def cancel(self, timer_id):
        self.cancelled.append(timer_id)
        self.pending.pop(timer_id, None)

    def one_shots(self):
        return [
            timer_id for timer_id, (delay, _callback) in self.pending.items()
            if delay == adapter_class_supervisor.BUSY_RETRY_SECONDS
        ]

    def fire_one_shot(self):
        (timer_id,) = self.one_shots()
        _delay, callback = self.pending.pop(timer_id)
        return callback()

    def tick(self):
        (callback,) = [
            callback for delay, callback in self.pending.values()
            if delay == adapter_class_supervisor.RECONCILE_SECONDS
        ]
        return callback()


def _busy_supervisor(outcomes, attempts, timers, now, state=None):
    from blueferry.bluez_setup import CodAdapterBusy

    state = state if state is not None else {"class": 0x000000}

    def repair(adapter):
        attempts.append(adapter)
        outcome = outcomes.pop(0) if outcomes else "busy"
        if outcome == "busy":
            raise CodAdapterBusy("busy")
        state["class"] = 0x408
        return True

    return AdapterClassSupervisor(
        "hci7",
        read_class=lambda _adapter: state["class"],
        matches=lambda value: value == 0x408,
        repair=repair,
        schedule=timers.schedule,
        cancel=timers.cancel,
        clock=lambda: now[0],
        uses_sudo=lambda: True,
    )


def test_busy_adapter_is_retried_after_seconds_without_backoff() -> None:
    attempts, timers, now, state = [], _Timers(), [0.0], {"class": 0}
    supervisor = _busy_supervisor(["busy", "ok"], attempts, timers, now, state)

    supervisor.start()
    assert attempts == ["hci7"]
    assert len(timers.one_shots()) == 1

    now[0] += adapter_class_supervisor.BUSY_RETRY_SECONDS
    assert timers.fire_one_shot() is False

    assert attempts == ["hci7", "hci7"]
    assert state["class"] == 0x408
    assert supervisor._retry_at is None
    assert supervisor._busy_retries == 0
    assert timers.one_shots() == []


def test_busy_retries_are_bounded_then_back_off() -> None:
    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor([], attempts, timers, now)

    supervisor.start()
    for _retry in range(adapter_class_supervisor.MAX_BUSY_RETRIES):
        now[0] += adapter_class_supervisor.BUSY_RETRY_SECONDS
        assert timers.fire_one_shot() is False

    assert len(attempts) == 1 + adapter_class_supervisor.MAX_BUSY_RETRIES
    assert timers.one_shots() == []
    assert supervisor._retry_at == (
        now[0] + adapter_class_supervisor.FAILED_REPAIR_BACKOFF_SECONDS
    )

    # The usual sudo backoff now applies: no attempt within the minute.
    now[0] += 30
    assert timers.tick() is True
    assert len(attempts) == 1 + adapter_class_supervisor.MAX_BUSY_RETRIES


def test_busy_retry_never_plans_a_second_timer() -> None:
    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor([], attempts, timers, now)

    supervisor.start()
    # The periodic tick meets Busy again while the one-shot is pending.
    now[0] += 1
    assert timers.tick() is True

    assert len(attempts) == 2
    assert len(timers.one_shots()) == 1


def test_stop_cancels_the_busy_retry() -> None:
    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor([], attempts, timers, now)
    supervisor.start()
    (busy_timer,) = timers.one_shots()

    supervisor.stop()

    assert busy_timer in timers.cancelled
    assert timers.pending == {}
    assert supervisor._busy_retries == 0


def test_busy_retry_after_stop_does_nothing() -> None:
    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor([], attempts, timers, now)
    supervisor.start()
    (busy_timer,) = timers.one_shots()
    _delay, callback = timers.pending[busy_timer]
    supervisor.stop()

    assert callback() is False
    assert attempts == ["hci7"]


def test_poke_resets_the_busy_budget() -> None:
    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor([], attempts, timers, now)
    supervisor.start()
    for _retry in range(adapter_class_supervisor.MAX_BUSY_RETRIES):
        assert timers.fire_one_shot() is False
    assert supervisor._retry_at is not None

    supervisor.poke()

    assert supervisor._busy_retries == 1
    assert len(timers.one_shots()) == 1


def test_busy_logs_one_warning_and_info_retries(caplog) -> None:
    import logging

    attempts, timers, now = [], _Timers(), [0.0]
    supervisor = _busy_supervisor(["busy", "busy", "ok"], attempts, timers, now)

    with caplog.at_level(logging.DEBUG, logger=adapter_class_supervisor.__name__):
        supervisor.start()
        timers.fire_one_shot()
        timers.fire_one_shot()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    busy = [r for r in caplog.records if "adapter busy; retrying in 3 s" in r.getMessage()]
    assert len(warnings) == 1
    assert len(busy) == 2
    assert all(r.levelno == logging.INFO for r in busy)
