"""ANCS solicitation supervision without Bluetooth or D-Bus access."""

from blueferry import solicitation_supervisor
from blueferry.solicitation_supervisor import SolicitationSupervisor


def _supervisor(
    calls,
    state,
    scheduled,
    *,
    needed=True,
    minimum_on_seconds=0,
    clock=lambda: 0.0,
):
    return SolicitationSupervisor(
        "hci7",
        needed=needed,
        register=lambda adapter: (
            calls.append(("register", adapter)),
            state.__setitem__("registered", True),
            True,
        )[-1],
        unregister=lambda adapter: (
            calls.append(("unregister", adapter)),
            state.__setitem__("registered", False),
        ),
        is_registered=lambda: state["registered"],
        forget_registration=lambda: (
            calls.append("forget"),
            state.__setitem__("registered", False),
        ),
        minimum_on_seconds=minimum_on_seconds,
        schedule=lambda delay, callback: scheduled.append((delay, callback)) or 7,
        cancel=lambda timer_id: calls.append(("cancel", timer_id)),
        clock=clock,
    )


def test_solicitation_stays_on_until_ancs_is_healthy() -> None:
    calls = []
    state = {"registered": False}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)

    supervisor.start()
    assert calls == [("register", "hci7")]
    assert supervisor.active() is True
    assert scheduled[0][0] == solicitation_supervisor.RECONCILE_SECONDS

    supervisor.set_needed(False)
    assert calls[-1] == ("unregister", "hci7")

    supervisor.set_needed(True)
    assert calls[-1] == ("register", "hci7")


def test_periodic_reconciliation_repairs_a_bluez_release() -> None:
    calls = []
    state = {"registered": True}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()
    assert calls == []

    state["registered"] = False
    assert scheduled[0][1]() is True
    assert calls == [("register", "hci7")]


def test_healthy_ancs_keeps_post_pair_solicitation_for_permission_window() -> None:
    calls = []
    state = {"registered": False}
    scheduled = []
    now = 0.0
    supervisor = _supervisor(
        calls,
        state,
        scheduled,
        minimum_on_seconds=180,
        clock=lambda: now,
    )
    supervisor.start()

    supervisor.set_needed(False)
    assert state["registered"] is True

    now = 180.0
    scheduled[0][1]()
    assert calls[-1] == ("unregister", "hci7")


def test_outbound_dial_withdraws_and_then_restores_solicitation() -> None:
    calls = []
    state = {"registered": False}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()

    supervisor.set_dialing(True)
    assert calls[-1] == ("unregister", "hci7")
    assert supervisor.active() is False

    supervisor.set_dialing(False)
    assert calls[-1] == ("register", "hci7")
    assert supervisor.active() is True


def test_solicitation_stays_withdrawn_when_need_changes_during_dial() -> None:
    calls = []
    state = {"registered": False}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()
    supervisor.set_dialing(True)

    supervisor.set_needed(False)
    supervisor.set_needed(True)

    assert state["registered"] is False
    assert calls == [
        ("register", "hci7"),
        ("unregister", "hci7"),
    ]

    supervisor.set_dialing(False)
    assert calls[-1] == ("register", "hci7")


def test_bluez_restart_forgets_registration_and_primes_inbound_le() -> None:
    calls = []
    state = {"registered": False}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled, needed=False)
    supervisor.start()

    supervisor.reset_after_bluez_restart()

    assert supervisor.needed is True
    assert calls == ["forget", ("register", "hci7")]


def test_stop_cancels_reconciliation_and_removes_advertisement() -> None:
    calls = []
    state = {"registered": True}
    scheduled = []
    supervisor = _supervisor(calls, state, scheduled)
    supervisor.start()

    supervisor.stop()

    assert calls == [("cancel", 7), ("unregister", "hci7")]
    assert scheduled[0][1]() is False
