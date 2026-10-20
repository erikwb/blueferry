"""Short LE drops reach ANCS and AMS from BlueZ's signals; fakes only.

On hardware (iOS 27, BlueZ 5.87, 2026-10-08) the LE link dropped for 0.5 s
and came back between two of the bearer supervisor's five-second polls.
Polling never saw the cycle, so neither client rebuilt what iOS had
forgotten with the link.
"""

from __future__ import annotations

from types import SimpleNamespace

from blueferry.ams.client import AmsClient
from blueferry.ams.constants import EntityID
from tests.test_ams_client import EU, RC, _Bus, _objects, _Timers
from tests.test_le_bond_detection import LE, TIMEOUT, _Harness


def _drop_and_return(h: _Harness, *, down_for: float = 0.5) -> None:
    """One LE cycle the way BlueZ 5.87 signals it, with no poll in between."""
    h.on_properties(LE, {"Connected": False}, [])
    h.on_disconnected(TIMEOUT, "Connection timeout")
    h.clock.now += down_for
    h.on_properties(LE, {"Connected": True}, [])


def _observed(h: _Harness) -> list:
    observed: list = []
    h.supervisor._on_le_state = observed.append
    return observed


def test_a_drop_between_two_polls_is_published_from_the_signals() -> None:
    h = _Harness(le=True)
    observed = _observed(h)
    h.supervisor.start()
    assert observed == [True]

    _drop_and_return(h)

    assert observed == [True, False, True]
    assert h.supervisor.le_connected
    # The next poll finds the link up again and reports nothing new.
    h.poll()
    assert observed == [True, False, True]


def test_the_disconnect_signal_alone_publishes_the_drop() -> None:
    # The property change may arrive after the signal, or after the link is
    # back (BlueZ reads it when it emits); the signal does not wait for it.
    h = _Harness(le=True)
    observed = _observed(h)
    h.supervisor.start()

    h.on_disconnected(TIMEOUT, "Connection timeout")
    assert observed == [True, False]
    h.on_properties(LE, {"Connected": False}, [])
    assert observed == [True, False]
    h.on_properties(LE, {"Connected": True}, [])
    assert observed == [True, False, True]


def test_local_and_suspend_drops_are_published_too() -> None:
    # They do not count towards the stale-bond report, but the link and its
    # GATT registrations are gone all the same.
    for reason in ("org.bluez.Reason.Local", "org.bluez.Reason.Suspend"):
        h = _Harness(le=True)
        observed = _observed(h)
        h.supervisor.start()

        h.on_disconnected(reason, "")

        assert observed == [True, False]
        assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_signals_after_stop_are_ignored() -> None:
    h = _Harness(le=True)
    observed = _observed(h)
    h.supervisor.start()
    h.supervisor.stop()

    _drop_and_return(h)

    assert observed == [True]


def test_ams_registers_again_after_a_drop_no_poll_saw() -> None:
    h = _Harness(le=True)
    bus = _Bus()
    timers = _Timers()
    client = AmsClient(
        "/org/bluez/hci0/dev_02_00_00_00_00_01",
        on_update=lambda _update: None,
        on_supported_commands=lambda _commands: None,
        bus_factory=lambda: bus,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    rc = RC.replace("dev_AA_BB_CC_DD_EE_FF", "dev_02_00_00_00_00_01")
    eu = EU.replace("dev_AA_BB_CC_DD_EE_FF", "dev_02_00_00_00_00_01")
    objects = {
        path.replace("dev_AA_BB_CC_DD_EE_FF", "dev_02_00_00_00_00_01"): value
        for path, value in _objects().items()
    }
    h.supervisor._on_le_state = client.observe_bearer_state
    h.supervisor.start()
    client.start()
    bus.take("GetManagedObjects").succeed(objects)
    timers.run_all()  # bearer settle
    bus.take("StartNotify", rc).succeed()
    bus.take("StartNotify", eu).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", eu).succeed()
    assert client.available

    _drop_and_return(h)

    assert not client.available
    timers.run_all()  # bearer settle on the new link
    # BlueZ kept the notify sessions; iOS forgot the registrations.
    bus.take("Get", rc).succeed(True)
    bus.take("Get", eu).succeed(True)
    for _entity in EntityID:
        bus.take("WriteValue", eu).succeed()
    assert client.available


def test_the_daemon_hands_a_signalled_drop_to_ancs_and_ams(make_daemon) -> None:
    instance = make_daemon()
    h = _Harness(le=True)
    instance.bearers = h.supervisor
    h.supervisor._on_le_state = instance._observe_le_state
    ancs: list = []
    ams: list = []
    instance.ancs = SimpleNamespace(
        observe_bearer_state=lambda connected, **_kwargs: ancs.append(connected),
        connected=False,
    )
    instance.ams = SimpleNamespace(observe_bearer_state=ams.append)
    h.supervisor.start()

    _drop_and_return(h)

    assert ancs == [True, False, True]
    assert ams == [True, False, True]
