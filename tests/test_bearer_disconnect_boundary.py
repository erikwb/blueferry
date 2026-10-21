"""A link-state change must not complete an outstanding Disconnect call."""

from itertools import count
from types import SimpleNamespace

import dbus
import pytest

from blueferry.bearer_supervisor import CLASSIC_SETTLE_SECONDS, BearerSupervisor


@pytest.fixture
def rig():
    state = {"bredr": True, "le": True}
    now = [0.0]
    connects, disconnects, preferences, observed = [], [], [], []
    timers = {}
    timer_ids = count(1)

    def schedule(delay, callback):
        timer_id = next(timer_ids)
        timers[timer_id] = (delay, callback)
        return timer_id

    def settle():
        for timer_id, (delay, callback) in list(timers.items()):
            if delay == CLASSIC_SETTLE_SECONDS:
                timers.pop(timer_id)
                callback()

    supervisor = BearerSupervisor(
        "/device",
        read_connected=state.get,
        connect=lambda kind, success, error: connects.append((kind, success, error)),
        disconnect=lambda kind, success, error: disconnects.append((kind, success, error)),
        prefer=preferences.append,
        on_le_state=observed.append,
        clock=lambda: now[0],
        schedule=schedule,
        cancel=lambda timer_id: timers.pop(timer_id, None),
    )
    supervisor.start()
    yield SimpleNamespace(
        supervisor=supervisor, state=state, now=now, connects=connects,
        disconnects=disconnects, preferences=preferences, observed=observed,
        timers=timers, settle=settle,
    )
    supervisor.stop()


@pytest.mark.parametrize("next_bearer", ["le", "bredr"])
@pytest.mark.parametrize("outcome", ["success", "not_connected", "failure", "timeout"])
def test_disconnect_reply_gates_reconnect_after_early_link_down(rig, next_bearer, outcome):
    rig.supervisor.recover_le_transport()
    assert len(rig.disconnects) == 1
    _, success, error = rig.disconnects[0]

    rig.state["le"] = False
    rig.state["bredr"] = next_bearer == "le"
    rig.now[0] = 5.0
    rig.supervisor.poke()
    assert rig.observed == [True, False]  # Continue publishing real link changes.
    rig.now[0] = 8.0
    rig.settle()
    # Let the recovery budget's existing cooldown expire, while the 45-second
    # Disconnect call is still outstanding. Elapsed time cannot open the gate.
    rig.now[0] = 30.0
    rig.supervisor.poke()
    rig.now[0] = 33.0
    rig.settle()
    assert rig.connects == []
    assert rig.preferences == []
    assert rig.supervisor.busy

    if outcome == "success":
        success()
    else:
        name = {
            "not_connected": "org.bluez.Error.NotConnected",
            "failure": "org.bluez.Error.Failed",
            "timeout": "org.freedesktop.DBus.Error.NoReply",
        }[outcome]
        error(dbus.exceptions.DBusException("test disconnect result", name=name))

    # Completed calls return to the existing recovery policy, including errors
    # and local D-Bus timeouts; none leaves a permanent pending-call latch.
    rig.supervisor.poke()
    rig.now[0] = 36.0
    rig.settle()
    assert [kind for kind, *_callbacks in rig.connects] == [next_bearer]
    assert len(rig.disconnects) == 1


@pytest.mark.parametrize("next_bearer", ["le", "bredr"])
def test_already_queued_connect_callback_cannot_bypass_disconnect(rig, next_bearer):
    rig.state["le"] = False
    rig.supervisor.poke()
    queued = next(
        callback for delay, callback in rig.timers.values()
        if delay == CLASSIC_SETTLE_SECONDS
    )
    rig.supervisor.recover_le_transport(allow_disconnected=True)
    assert len(rig.disconnects) == 1
    rig.supervisor.hold_le()
    rig.supervisor.enable_le()
    rig.state["bredr"] = next_bearer == "le"
    rig.now[0] = 33.0

    # A source can already be queued for dispatch when it is cancelled.
    queued()
    assert rig.connects == []
    assert rig.preferences == []

    rig.disconnects[0][1]()
    rig.supervisor.poke()
    rig.settle()
    assert [kind for kind, *_callbacks in rig.connects] == [next_bearer]
    assert rig.preferences == [next_bearer]


def test_inbound_le_is_observed_while_disconnect_reply_is_pending(rig):
    rig.supervisor.recover_le_transport()
    rig.state["le"] = False
    rig.supervisor.poke()
    rig.state["le"] = True
    rig.supervisor.poke()
    assert rig.observed == [True, False, True]
    assert rig.connects == []
    assert len(rig.disconnects) == 1

    rig.disconnects[0][1]()
    rig.now[0] = 33.0
    rig.supervisor.poke()
    rig.settle()
    assert rig.supervisor.le_connected
    assert rig.connects == []
    assert len(rig.disconnects) == 1


@pytest.mark.parametrize("old_reply", ["success", "failure"])
def test_old_bluez_reply_cannot_release_a_new_disconnect(rig, old_reply):
    rig.supervisor.recover_le_transport()
    _, old_success, old_error = rig.disconnects[0]
    rig.supervisor.reset_after_bluez_restart()
    rig.now[0] = 30.0
    rig.supervisor.recover_le_transport()
    assert len(rig.disconnects) == 2
    rig.state["le"] = False
    rig.supervisor.poke()

    if old_reply == "success":
        old_success()
    else:
        old_error(RuntimeError("old daemon disappeared"))
    rig.now[0] = 90.0
    rig.supervisor.poke()
    rig.settle()
    assert rig.connects == []
    assert rig.supervisor.busy

    rig.disconnects[1][1]()
    rig.supervisor.poke()
    rig.settle()
    assert [kind for kind, *_callbacks in rig.connects] == ["le"]
