"""ANCS end to end against a bluetoothd without bearer interfaces."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import dbus
import pytest
from gi.repository import GLib

from blueferry import bearer_supervisor
from blueferry.ancs import client as client_module
from blueferry.ancs.client import AncsClient
from blueferry.bearer_supervisor import BearerSupervisor

pytestmark = pytest.mark.private_dbus

PEER = Path(__file__).with_name("legacy_bluez_peer.py")
DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def _dispatch_until(done, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while not done() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.001)
    assert done(), "the legacy BlueZ peer did not respond"


class _Timers:
    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, object]] = {}
        self.serial = 0

    def schedule(self, delay, callback) -> int:
        self.serial += 1
        self.pending[self.serial] = (delay, callback)
        return self.serial

    def cancel(self, source) -> None:
        self.pending.pop(source, None)

    def delays(self, name: str) -> list[int]:
        return [
            delay for delay, callback in self.pending.values()
            if callback.__name__ == name
        ]

    def fire(self, name: str) -> None:
        [source] = [
            source for source, (_delay, callback) in self.pending.items()
            if callback.__name__ == name
        ]
        delay, callback = self.pending.pop(source)
        if callback():  # A GLib source repeats while its callback is true.
            self.pending[source] = (delay, callback)


@pytest.fixture
def legacy(monkeypatch):
    process = subprocess.Popen([sys.executable, str(PEER)])
    bus = dbus.SystemBus(private=True)
    bus.set_exit_on_disconnect(False)
    timers = _Timers()
    client = supervisor = None
    try:
        deadline = time.monotonic() + 10
        while not bus.name_has_owner("org.bluez"):
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        peer = dbus.Interface(
            bus.get_object("org.bluez", "/", introspect=False),
            "io.weirdware.BlueFerry.TestPeer",
        )
        monkeypatch.setattr(bearer_supervisor, "get_system_bus", lambda: bus)
        monkeypatch.setattr(client_module, "get_system_bus", lambda: bus)
        monkeypatch.setattr(client_module.GLib, "timeout_add_seconds", timers.schedule)
        monkeypatch.setattr(client_module.GLib, "source_remove", timers.cancel)
        statuses = []
        client = AncsClient(
            DEVICE,
            lambda _event: None,
            on_status=lambda: statuses.append(client.connected),
            schedule=timers.schedule,
            cancel=timers.cancel,
        )
        supervisor = BearerSupervisor(
            DEVICE,
            on_le_state=lambda state: client.observe_bearer_state(
                state, legacy_connected=supervisor.legacy_connected,
            ),
            connect=lambda *_args: None,
            schedule=timers.schedule,
            cancel=timers.cancel,
        )
        yield peer, client, supervisor, timers, statuses
    finally:
        if supervisor is not None:
            supervisor.stop()
        if client is not None:
            client.stop()
        process.terminate()
        process.wait(10)
        bus.close()


def test_ancs_follows_an_le_link_that_bluez_cannot_report(legacy):
    peer, client, supervisor, timers, statuses = legacy
    peer.SetClassic(True)

    supervisor.start()
    client.start()
    assert supervisor.le_state is None and supervisor.legacy_connected
    timers.fire("_finish_bearer_settle")

    # Classic alone: both registrations are left pending and the probe fails
    # locally. A retry repeats StartNotify, which is InProgress, not an error.
    assert not client.connected
    timers.fire("_retry_subscribe")
    assert not client.connected
    assert tuple(peer.Counters()) == (4, 0, 0)
    assert timers.delays("_retry_subscribe") == [4]

    # The phone's solicited LE link arrives. The next poll sees only
    # ServicesResolved change and probes without waiting out the backoff.
    peer.SetLe(True)
    timers.fire("_tick")
    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
    timers.fire("_retry_subscribe")
    _dispatch_until(lambda: client.connected)
    assert statuses == [True]

    # LE drops while Classic keeps Device1.Connected true.
    peer.SetLe(False)
    timers.fire("_tick")
    assert not client.connected and statuses == [True, False]
    assert supervisor.le_state is None and supervisor.legacy_connected

    peer.SetLe(True)
    timers.fire("_tick")
    timers.fire("_retry_subscribe")
    _dispatch_until(lambda: client.connected)

    # Nothing was torn down along the way.
    assert int(peer.Counters()[1]) == 0


def test_unanswered_ancs_keeps_asking_for_permission(legacy):
    peer, client, supervisor, timers, _statuses = legacy
    peer.SetClassic(True)
    peer.SetLe(True)
    peer.SetAnswering(False)

    supervisor.start()
    client.start()
    timers.fire("_finish_bearer_settle")

    assert client.subscribed and not client.connected
    timers.fire("_request_timed_out")
    assert timers.delays("_retry_authorization") == [
        client_module.AUTHORIZATION_RETRY_SECONDS
    ]

    peer.SetAnswering(True)
    timers.fire("_retry_authorization")
    _dispatch_until(lambda: client.connected)
    assert int(peer.Counters()[1]) == 0
