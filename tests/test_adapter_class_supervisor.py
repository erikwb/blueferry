"""Adapter Class-of-Device supervision without Bluetooth or systemd."""

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
