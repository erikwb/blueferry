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
    LE_FLAP_THRESHOLD,
    LE_FLAP_WINDOW_SECONDS,
    POLL_SECONDS,
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


def test_burst_of_short_le_links_marks_the_bond_suspect_once(caplog) -> None:
    caplog.set_level(logging.INFO, logger="blueferry.bearer_supervisor")
    h = _Harness()
    h.supervisor.start()

    for _ in range(LE_FLAP_THRESHOLD - 1):
        h.flap()
    assert not h.supervisor.le_bond_suspect
    statuses = h.statuses

    h.flap()

    assert h.supervisor.le_bond_suspect
    assert h.statuses == statuses + 1
    snapshot = h.supervisor.snapshot()
    assert snapshot["le_bond_suspect"] is True
    assert snapshot["le_flap_count"] == LE_FLAP_THRESHOLD
    assert snapshot["last_le_disconnect_reason"] == "timeout"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bluetoothctl remove" in warnings[0].getMessage()
    assert "02:00" not in warnings[0].getMessage()
    assert "dev_02" not in warnings[0].getMessage()

    for _ in range(20):
        h.flap()

    assert h.supervisor.snapshot()["le_flap_count"] == LE_FLAP_THRESHOLD + 20
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert h.statuses == statuses + 1


def test_suspect_bond_stops_outbound_le_dials() -> None:
    h = _Harness()
    h.supervisor.start()
    # Classic up and LE down: the supervisor arms its one LE bootstrap.
    settle_id, connect_le = h.settle()

    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()

    assert h.cancelled == [settle_id]
    connect_le()  # A timer that raced the cancellation still must not dial.
    for _ in range(3):
        h.clock.now += POLL_SECONDS
        h.poll()

    assert "le" not in h.connections
    assert len(h.scheduled) == 2


def test_suspect_bond_does_not_reset_the_le_transport() -> None:
    h = _Harness(le=True)
    h.supervisor.start()
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()

    h.supervisor.recover_le_transport(allow_disconnected=True)

    assert h.disconnections == []


def test_ancs_authorization_clears_the_suspicion_and_rearms_le() -> None:
    h = _Harness()
    h.supervisor.start()
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()
    statuses = h.statuses

    h.supervisor.note_le_usable("ANCS authorized")

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0
    assert h.statuses == statuses + 1
    h.clock.now += 600
    h.poll()
    h.scheduled[-1][1]()
    assert "le" in h.connections


def test_a_new_bond_clears_the_suspicion_but_a_removed_key_does_not() -> None:
    h = _Harness()
    h.supervisor.start()
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()

    h.on_properties(LE, {"Paired": False}, [])
    assert h.supervisor.le_bond_suspect

    h.on_properties("org.bluez.Device1", {"Bonded": True}, [])
    assert not h.supervisor.le_bond_suspect


def test_walking_away_and_back_is_not_a_broken_bond() -> None:
    h = _Harness()
    h.supervisor.start()

    for _ in range(3 * LE_FLAP_THRESHOLD):
        # A healthy link that holds, then a supervision timeout when the
        # user walks out of range, and a return a few seconds later.
        h.flap(up_for=STABLE_CONNECTION_SECONDS + 1, down_for=5)

    assert not h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["le_flap_count"] == 0


def test_a_held_link_between_short_drops_resets_the_count() -> None:
    h = _Harness()
    h.supervisor.start()

    for _ in range(3):
        for _ in range(LE_FLAP_THRESHOLD - 1):
            h.flap()
        h.flap(up_for=STABLE_CONNECTION_SECONDS + 1)

    assert not h.supervisor.le_bond_suspect


def test_sparse_drops_outside_the_window_are_not_a_burst() -> None:
    h = _Harness()
    h.supervisor.start()

    for _ in range(3 * LE_FLAP_THRESHOLD):
        h.flap(down_for=LE_FLAP_WINDOW_SECONDS / (LE_FLAP_THRESHOLD - 1))

    assert not h.supervisor.le_bond_suspect


def test_suspend_and_own_disconnects_are_not_counted() -> None:
    h = _Harness(le=True)
    h.supervisor.start()

    for _ in range(LE_FLAP_THRESHOLD):
        h.flap(reason="org.bluez.Reason.Suspend")
    assert h.supervisor.snapshot()["le_flap_count"] == 0
    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == "suspend"

    h.supervisor.recover_le_transport()
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap(reason="org.bluez.Reason.Local", up_for=0.5, down_for=0.5)
    assert not h.supervisor.le_bond_suspect


def test_unrecognized_reason_names_are_not_published() -> None:
    h = _Harness()
    h.supervisor.start()

    h.flap(reason="org.example.Reason.Name at /org/bluez/hci0/dev_02_00")

    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == "unknown"


def test_polling_fallback_detects_flaps_without_the_signal() -> None:
    h = _Harness(watch=False)
    h.supervisor.start()

    for _ in range(LE_FLAP_THRESHOLD):
        h.state["le"] = True
        h.clock.now += POLL_SECONDS
        h.poll()
        h.state["le"] = False
        h.clock.now += POLL_SECONDS
        h.poll()

    assert h.supervisor.le_bond_suspect
    assert h.supervisor.snapshot()["last_le_disconnect_reason"] == ""


def test_polling_fallback_ignores_a_link_that_stays_up() -> None:
    h = _Harness(watch=False)
    h.supervisor.start()

    for _ in range(LE_FLAP_THRESHOLD):
        h.state["le"] = True
        for _ in range(STABLE_CONNECTION_SECONDS // POLL_SECONDS + 1):
            h.clock.now += POLL_SECONDS
            h.poll()
        h.state["le"] = False
        h.clock.now += POLL_SECONDS
        h.poll()

    assert not h.supervisor.le_bond_suspect


def test_polling_that_samples_only_up_states_does_not_hide_flaps() -> None:
    # The phone's two-second cycle can put every five-second probe on an
    # up phase. Only BlueZ's own link transitions may prove a held link.
    h = _Harness(le=True)
    h.supervisor.start()

    for _ in range(4 * LE_FLAP_THRESHOLD):
        h.flap(up_for=1.5, down_for=1.0)
        h.poll()

    assert h.supervisor.le_bond_suspect


def test_bluez_restart_and_stop_manage_detection_state() -> None:
    h = _Harness()
    h.supervisor.start()
    for _ in range(LE_FLAP_THRESHOLD):
        h.flap()

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
        pump(lambda: supervisor.le_bond_suspect)

        assert supervisor.le_bond_suspect
        assert supervisor.snapshot()["le_flap_count"] == LE_FLAP_THRESHOLD
        assert supervisor.snapshot()["last_le_disconnect_reason"] == "timeout"

        emit(
            device, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            "sa{sv}as", LE, {"Bonded": True}, [],
        )
        pump(lambda: not supervisor.le_bond_suspect)
        assert not supervisor.le_bond_suspect
    finally:
        supervisor.stop()
        server.release_name("org.bluez")
        server.close()
        monitor.close()
