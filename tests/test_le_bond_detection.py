"""Stale LE bond detection; fakes only, apart from one private-bus test."""

from __future__ import annotations

import logging
import time

import dbus
import dbus.lowlevel
import pytest
from gi.repository import GLib

from blueferry import bearer_supervisor
from blueferry.bearer_supervisor import (
    LE_FLAP_MAX_LINK_SECONDS,
    LE_FLAP_PERSIST_SECONDS,
    LE_FLAP_THRESHOLD,
    LE_FLAP_WINDOW_SECONDS,
    LE_SUSPECT_ABSENT_EXPIRY_SECONDS,
    POLL_SECONDS,
    POLLED_LE_FLAP_PERSIST_SECONDS,
    STABLE_CONNECTION_SECONDS,
    BearerSupervisor,
)

TIMEOUT = "org.bluez.Reason.Timeout"
LE = "org.bluez.Bearer.LE1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Harness:
    """One supervisor with fake BlueZ state, timers, and signal watch."""

    def __init__(self, *, bredr=True, le=False, le_enabled=True, watch=True):
        self.state = {"bredr": bredr, "le": le}
        self.clock = _Clock()
        self.connections: list[str] = []
        self.disconnections: list[str] = []
        self.scheduled: list[tuple[int, object]] = []
        self.cancelled: list[int] = []
        self.statuses = 0
        self.on_disconnected = None
        self.on_properties = None
        self.unwatched = 0

        def watch_le(on_disconnected, on_properties):
            self.on_disconnected = on_disconnected
            self.on_properties = on_properties

            def unwatch():
                self.unwatched += 1

            return unwatch

        def schedule(delay, callback):
            self.scheduled.append((delay, callback))
            return len(self.scheduled)

        def status():
            self.statuses += 1

        self.supervisor = BearerSupervisor(
            "/org/bluez/hci0/dev_02_00_00_00_00_01",
            le_enabled=le_enabled,
            on_status=status,
            read_connected=self.state.get,
            connect=lambda kind, on_success, _on_error: (
                self.connections.append(kind),
                on_success(),
            ),
            disconnect=lambda kind, on_success, _on_error: (
                self.disconnections.append(kind),
                on_success(),
            ),
            watch_le=watch_le if watch else None,
            schedule=schedule,
            cancel=self.cancelled.append,
            clock=self.clock,
        )

    def poll(self) -> None:
        next(cb for delay, cb in self.scheduled if delay == POLL_SECONDS)()

    def settle(self):
        """Return (timer id, callback) of the armed LE settle timer."""
        return next(
            (index + 1, cb)
            for index, (delay, cb) in enumerate(self.scheduled)
            if delay == bearer_supervisor.CLASSIC_SETTLE_SECONDS
        )

    def link_up(self) -> None:
        self.on_properties(LE, {"Connected": True}, [])

    def flap(self, *, reason=TIMEOUT, up_for=1.5, down_for=0.5) -> None:
        """One short-lived LE link as seen in the btmon trace."""
        self.link_up()
        self.clock.now += up_for
        self.on_properties(LE, {"Connected": False}, [])
        self.on_disconnected(reason, "Connection timeout")
        self.clock.now += down_for


def _burst(h, seconds, **flap):
    """Flap for ``seconds`` of fake time at the btmon trace's rate."""
    until = h.clock.now + seconds
    while h.clock.now < until:
        h.flap(**flap)


def _poll_for(h, seconds):
    until = h.clock.now + seconds
    while h.clock.now < until:
        h.clock.now += POLL_SECONDS
        h.poll()


def test_persistent_burst_of_short_le_links_marks_the_bond_suspect_once(caplog) -> None:
    caplog.set_level(logging.INFO, logger="blueferry.bearer_supervisor")
    h = _Harness()
    h.supervisor.start()

    # The rate threshold alone is not enough any more.
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()
    assert not h.supervisor.le_bond_suspect
    _burst(h, LE_FLAP_PERSIST_SECONDS - 20)
    assert not h.supervisor.le_bond_suspect
    statuses = h.statuses

    _burst(h, 30)

    assert h.supervisor.le_bond_suspect
    assert h.statuses == statuses + 1
    snapshot = h.supervisor.snapshot()
    assert snapshot["le_bond_suspect"] is True
    assert snapshot["le_flap_count"] > LE_FLAP_THRESHOLD
    assert snapshot["last_le_disconnect_reason"] == "timeout"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "bluetoothctl remove" in message
    assert "may help" in message
    assert "02:00" not in message
    assert "dev_02" not in message

    count = snapshot["le_flap_count"]
    for _ in range(20):
        h.flap()

    assert h.supervisor.snapshot()["le_flap_count"] == count + 20
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert h.statuses == statuses + 1


@pytest.mark.parametrize("reason", [TIMEOUT, "org.bluez.Reason.Remote",
                                    "org.bluez.Reason.Authentication"])
def test_counted_reasons(reason) -> None:
    h = _Harness()
    h.supervisor.start()

    _burst(h, LE_FLAP_PERSIST_SECONDS + 10, reason=reason)

    assert h.supervisor.le_bond_suspect


@pytest.mark.parametrize("reason", ["org.bluez.Reason.Unknown",
                                    "org.bluez.Reason.Local",
                                    "org.bluez.Reason.Suspend"])
def test_local_unknown_and_suspend_drops_never_count(reason) -> None:
    # Local covers rfkill, adapter power and BlueFerry's own resets alike.
    h = _Harness()
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS, reason=reason)

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0


@pytest.mark.parametrize("up_for", [LE_FLAP_MAX_LINK_SECONDS + 1, 11, 14])
def test_moderately_short_links_are_benign_flapping(up_for) -> None:
    h = _Harness()
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS, up_for=up_for, down_for=1)

    assert not h.supervisor.le_bond_suspect


def test_links_of_unknown_age_do_not_count_with_the_signal() -> None:
    h = _Harness()
    h.supervisor.start()
    until = h.clock.now + 3 * LE_FLAP_PERSIST_SECONDS
    while h.clock.now < until:
        h.on_disconnected(TIMEOUT, "Connection timeout")
        h.clock.now += 2

    assert not h.supervisor.le_bond_suspect


def test_benign_flapping_without_classic_is_not_a_broken_bond() -> None:
    # Five 2 s links 12 s apart with Classic down: a phone at the edge of
    # range, not demonstrably nearby.
    h = _Harness(bredr=False)
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS, up_for=2, down_for=10)

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_classic_dropping_during_the_burst_starts_it_over() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS - 10)

    h.state["bredr"] = False
    h.poll()
    h.state["bredr"] = True
    h.poll()
    _burst(h, 30)

    assert not h.supervisor.le_bond_suspect
    _burst(h, LE_FLAP_PERSIST_SECONDS)
    assert h.supervisor.le_bond_suspect


def test_a_quiet_gap_ends_the_burst() -> None:
    h = _Harness()
    h.supervisor.start()
    for _ in range(4):
        _burst(h, LE_FLAP_PERSIST_SECONDS / 2)
        h.clock.now += LE_FLAP_WINDOW_SECONDS + 1

    assert not h.supervisor.le_bond_suspect


def test_flap_count_decays_when_drops_stop() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, 30)
    assert h.supervisor.snapshot()["le_flap_count"] > 0

    _poll_for(h, LE_FLAP_WINDOW_SECONDS + POLL_SECONDS)

    assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_suspicion_expires_while_the_phone_is_away() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)
    assert h.supervisor.le_bond_suspect
    statuses = h.statuses

    h.state["bredr"] = False
    _poll_for(h, LE_SUSPECT_ABSENT_EXPIRY_SECONDS - POLL_SECONDS)
    assert h.supervisor.le_bond_suspect
    _poll_for(h, 2 * POLL_SECONDS)

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0
    assert h.statuses > statuses
    # Eight hours away keep it cleared.
    _poll_for(h, 8 * 3600)
    assert not h.supervisor.le_bond_suspect


def test_a_short_classic_hiccup_keeps_the_report() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)

    h.state["bredr"] = False
    _poll_for(h, 2 * POLL_SECONDS)
    h.state["bredr"] = True
    _poll_for(h, LE_SUSPECT_ABSENT_EXPIRY_SECONDS)

    assert h.supervisor.le_bond_suspect


def test_detection_is_off_when_it_does_not_apply() -> None:
    applies = {"value": False}
    h = _Harness()
    h.supervisor._le_bond_detection = lambda: applies["value"]
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS)
    assert not h.supervisor.le_bond_suspect

    applies["value"] = True
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)
    assert h.supervisor.le_bond_suspect

    applies["value"] = False
    h.poll()
    assert not h.supervisor.le_bond_suspect


def test_detection_is_off_while_le_is_held() -> None:
    h = _Harness(le_enabled=False)
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS)

    assert not h.supervisor.le_bond_suspect


def test_suspect_bond_does_not_change_connection_behaviour() -> None:
    """Report only: dials, the settle timer and LE resets stay as they were."""
    h = _Harness()
    h.supervisor.start()
    settle_id, connect_le = h.settle()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)
    assert h.supervisor.le_bond_suspect

    assert settle_id not in h.cancelled
    connect_le()
    assert "le" in h.connections

    h.state["le"] = True
    h.poll()
    h.supervisor.recover_le_transport(allow_disconnected=True)
    assert h.disconnections == ["le"]


def test_ancs_authorization_clears_the_suspicion() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)
    statuses = h.statuses

    h.supervisor.note_le_usable("ANCS authorized")

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0
    assert h.statuses == statuses + 1


def test_a_new_bond_clears_the_suspicion_but_a_removed_key_does_not() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)

    h.on_properties(LE, {"Paired": False}, [])
    assert h.supervisor.le_bond_suspect

    h.on_properties("org.bluez.Device1", {"Bonded": True}, [])
    assert not h.supervisor.le_bond_suspect


def test_walking_away_and_back_is_not_a_broken_bond() -> None:
    h = _Harness()
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS, up_for=STABLE_CONNECTION_SECONDS + 1,
           down_for=5)

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_a_held_link_between_short_drops_resets_the_burst() -> None:
    h = _Harness()
    h.supervisor.start()

    for _ in range(6):
        _burst(h, LE_FLAP_PERSIST_SECONDS / 2)
        h.flap(up_for=STABLE_CONNECTION_SECONDS + 1)

    assert not h.supervisor.le_bond_suspect


def test_sparse_drops_outside_the_window_are_not_a_burst() -> None:
    h = _Harness()
    h.supervisor.start()

    _burst(h, 3 * LE_FLAP_PERSIST_SECONDS,
           down_for=LE_FLAP_WINDOW_SECONDS / (LE_FLAP_THRESHOLD - 1))

    assert not h.supervisor.le_bond_suspect


def test_own_disconnects_are_not_reported_as_last_reason() -> None:
    h = _Harness(le=True)
    h.supervisor.start()

    h.supervisor.recover_le_transport()
    h.flap(reason="org.bluez.Reason.Local", up_for=0.5, down_for=0.5)

    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == ""


def test_unrecognized_reason_names_are_not_published() -> None:
    h = _Harness()
    h.supervisor.start()

    h.flap(reason="org.example.Reason.Name at /org/bluez/hci0/dev_02_00")

    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == "unknown"


def _polled_flaps(h, seconds, *, up_polls=1):
    until = h.clock.now + seconds
    while h.clock.now < until:
        h.state["le"] = True
        for _ in range(up_polls):
            h.clock.now += POLL_SECONDS
            h.poll()
        h.state["le"] = False
        h.clock.now += POLL_SECONDS
        h.poll()


def test_polling_fallback_detects_persistent_flaps_without_the_signal() -> None:
    h = _Harness(watch=False)
    h.supervisor.start()

    _polled_flaps(h, POLLED_LE_FLAP_PERSIST_SECONDS - 30)
    assert not h.supervisor.le_bond_suspect
    _polled_flaps(h, 60)

    assert h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == ""


def test_polling_fallback_ignores_a_link_that_stays_up() -> None:
    h = _Harness(watch=False)
    h.supervisor.start()

    _polled_flaps(h, 2 * POLLED_LE_FLAP_PERSIST_SECONDS,
                  up_polls=STABLE_CONNECTION_SECONDS // POLL_SECONDS + 1)

    assert not h.supervisor.le_bond_suspect


def test_polling_that_samples_only_up_states_does_not_hide_flaps() -> None:
    # The phone's two-second cycle can put every five-second probe on an
    # up phase. Only BlueZ's own link transitions may prove a held link.
    h = _Harness(le=True)
    h.supervisor.start()

    until = h.clock.now + LE_FLAP_PERSIST_SECONDS + 10
    while h.clock.now < until:
        h.flap(up_for=1.5, down_for=1.0)
        h.poll()

    assert h.supervisor.le_bond_suspect


def test_bluez_restart_and_stop_manage_detection_state() -> None:
    h = _Harness()
    h.supervisor.start()
    _burst(h, LE_FLAP_PERSIST_SECONDS + 10)

    h.supervisor.reset_after_bluez_restart()

    assert not h.supervisor.le_bond_suspect
    h.supervisor.stop()
    assert h.unwatched == 1
    h.on_disconnected(TIMEOUT, "late signal after stop")
    assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_watch_failure_falls_back_to_polling() -> None:
    def broken_watch(_on_disconnected, _on_properties):
        raise dbus.exceptions.DBusException("no bus")

    supervisor = BearerSupervisor(
        "/device",
        read_connected=lambda _kind: False,
        connect=lambda *_args: None,
        watch_le=broken_watch,
        schedule=lambda _delay, _callback: 1,
        cancel=lambda _source: None,
    )

    supervisor.start()

    assert supervisor.snapshot()["le_bond_suspect"] is False


@pytest.mark.private_dbus
def test_real_bearer_disconnected_signals_reach_the_supervisor(monkeypatch) -> None:
    """dbus-python match rules against a fake org.bluez on an isolated bus."""
    device = "/org/bluez/hci9/dev_02_00_00_00_00_01"
    server = dbus.SystemBus(private=True)
    monitor = dbus.SystemBus(private=True)
    server.request_name("org.bluez", dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    monkeypatch.setattr(bearer_supervisor, "get_system_bus", lambda: monitor)
    clock = _Clock()
    supervisor = BearerSupervisor(
        device,
        read_connected=lambda kind: kind == "bredr",
        connect=lambda *_args: None,
        watch_le=lambda on_disconnected, on_properties: (
            bearer_supervisor.watch_bluez_le(device, on_disconnected, on_properties)
        ),
        schedule=lambda _delay, _callback: 1,
        cancel=lambda _source: None,
        clock=clock,
    )

    def emit(path, interface, member, signature, *args):
        message = dbus.lowlevel.SignalMessage(path, interface, member)
        message.append(*args, signature=signature)
        server.send_message(message)

    def pump(predicate) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)

    try:
        supervisor.start()
        server.flush()
        monitor.flush()
        # Wrong device and wrong interface must not match.
        emit(
            "/org/bluez/hci9/dev_02_00_00_00_00_02", LE, "Disconnected", "ss",
            TIMEOUT, "Connection timeout",
        )
        emit(
            device, "org.bluez.Bearer.BREDR1", "Disconnected", "ss",
            TIMEOUT, "Connection timeout",
        )
        for _ in range(LE_FLAP_THRESHOLD):
            emit(
                device, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                "sa{sv}as", LE, {"Connected": True}, [],
            )
            emit(device, LE, "Disconnected", "ss", TIMEOUT, "Connection timeout")
        pump(lambda: supervisor.snapshot()["le_flap_count"] == LE_FLAP_THRESHOLD)

        # The fake clock stands still, so the burst has not persisted yet.
        assert not supervisor.le_bond_suspect
        assert supervisor.snapshot()["le_flap_count"] == LE_FLAP_THRESHOLD
        assert supervisor.snapshot()["last_le_disconnect_reason"] == "timeout"

        emit(
            device, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            "sa{sv}as", LE, {"Bonded": True}, [],
        )
        pump(lambda: supervisor.snapshot()["le_flap_count"] == 0)
        assert supervisor.snapshot()["le_flap_count"] == 0
    finally:
        supervisor.stop()
        server.release_name("org.bluez")
        server.close()
        monitor.close()
