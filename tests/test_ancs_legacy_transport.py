"""ANCS over a BlueZ that cannot report the LE bearer (before 5.84).

The fake characteristics follow bluetoothd's GATT client: StartNotify with
no ATT link succeeds and is completed on connection, a repeat meanwhile is
InProgress, and a write with no ATT link fails locally as "Not connected".
"""
from __future__ import annotations

import struct

import dbus
import pytest

from blueferry.ancs import client as client_module
from blueferry.ancs.client import AncsClient
from blueferry.ancs.constants import MESSAGES_APP_ID, CommandID

DEVICE = "/org/bluez/hci0/dev_AA"
NS, DS, CP = (f"{DEVICE}/service0001/char000{n}" for n in (1, 2, 3))


class _Gatt:
    """The three ANCS characteristics of one bonded, cached iPhone."""

    def __init__(self) -> None:
        self.le_up = False
        self.registered: set[str] = set()
        self.enabled: set[str] = set()
        self.notifying: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.writes = 0

    def connect_le(self) -> None:
        self.le_up = True
        self.enabled = set(self.registered)
        self.notifying |= self.enabled

    def drop_le(self) -> None:
        # bluetoothd keeps the registrations and the stale Notifying flag.
        self.le_up = False
        self.enabled.clear()

    def get_object(self, _name, path):
        return _Characteristic(self, path)

    def add_signal_receiver(self, *_args, **_kwargs):
        return _Match()


class _Match:
    def remove(self) -> None:
        pass


class _Characteristic:
    def __init__(self, gatt: _Gatt, path: str) -> None:
        self.gatt, self.path = gatt, path

    def Get(self, _interface, name, **_kwargs):
        assert name == "Notifying"
        return self.path in self.gatt.notifying

    def StartNotify(self, **_kwargs) -> None:
        gatt = self.gatt
        gatt.starts.append(self.path)
        if self.path in gatt.registered:
            if self.path in gatt.enabled:
                return
            raise dbus.exceptions.DBusException(
                "Operation already in progress", name="org.bluez.Error.InProgress",
            )
        gatt.registered.add(self.path)
        if gatt.le_up:
            gatt.enabled.add(self.path)
            gatt.notifying.add(self.path)

    def StopNotify(self, **_kwargs) -> None:
        self.gatt.stops.append(self.path)
        self.gatt.registered.discard(self.path)
        self.gatt.enabled.discard(self.path)
        self.gatt.notifying.discard(self.path)

    def WriteValue(self, _value, _options, **_kwargs) -> None:
        if not self.gatt.le_up:
            raise dbus.exceptions.DBusException(
                "Not connected", name="org.bluez.Error.Failed",
            )
        self.gatt.writes += 1


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
    gatt, timers, statuses, resets = _Gatt(), _Timers(), [], []
    monkeypatch.setattr(client_module, "get_system_bus", lambda: gatt)
    monkeypatch.setattr(client_module.dbus, "Interface", lambda value, _iface: value)
    monkeypatch.setattr(client_module.GLib, "timeout_add_seconds", timers.schedule)
    monkeypatch.setattr(client_module.GLib, "source_remove", timers.cancel)
    client = AncsClient(
        DEVICE,
        lambda _event: None,
        on_status=lambda: statuses.append(client.connected),
        on_transport_failure=lambda: resets.append(True),
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    client._started = True
    client._ns_path, client._ds_path, client._cp_path = NS, DS, CP
    client.gatt, client.timers, client.statuses, client.resets = (
        gatt, timers, statuses, resets,
    )
    return client


def _answer_probe(client: AncsClient) -> None:
    request = client._active_request
    assert request is not None and request.authorization_probe
    encoded = b"Messages"
    client._on_ds_changed("org.bluez.GattCharacteristic1", {"Value": (
        bytes([CommandID.GetAppAttributes]) + MESSAGES_APP_ID.encode() + b"\0"
        + bytes([0]) + struct.pack("<H", len(encoded)) + encoded
    )}, [])


def _connect(client: AncsClient) -> None:
    """Bring ANCS up over an LE link BlueZ cannot report."""
    client.gatt.connect_le()
    client.observe_bearer_state(None, legacy_connected=True)
    client.timers.fire("_finish_bearer_settle")
    _answer_probe(client)
    assert client.connected


def test_classic_only_connection_probes_with_backoff_and_no_bearer_reset(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers

    client.observe_bearer_state(None, legacy_connected=True)
    assert not gatt.starts  # The aggregate connection settles first.
    timers.fire("_finish_bearer_settle")

    # LE is down: registrations are left pending and the probe fails locally.
    assert gatt.registered == {NS, DS} and gatt.writes == 0
    assert not client.connected and not client.subscribed
    delays = []
    for _ in range(7):
        [delay] = timers.delays("_retry_subscribe")
        delays.append(delay)
        timers.fire("_retry_subscribe")
    assert delays == [2, 4, 8, 16, 32, 60, 60]
    assert gatt.stops == [] and client.resets == []
    assert "_request_transport_reset" not in [
        callback.__name__ for _delay, callback in timers.pending.values()
    ]


def test_pending_registrations_are_completed_by_the_phones_le_link(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    client.observe_bearer_state(None, legacy_connected=True)
    timers.fire("_finish_bearer_settle")
    assert not client.connected

    gatt.connect_le()  # The solicited link arrives; nothing reports it.
    timers.fire("_retry_subscribe")

    assert client.subscribed and not client.connected
    assert gatt.writes == 1
    _answer_probe(client)
    assert client.connected and client.statuses == [True]
    # Each characteristic was registered exactly once and never torn down.
    assert sorted(set(gatt.starts)) == [NS, DS] and gatt.stops == []
    assert timers.delays("_check_legacy_health") == [
        client_module.LEGACY_HEALTH_SECONDS
    ]


def test_aggregate_change_probes_at_once_instead_of_waiting_out_backoff(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    client.observe_bearer_state(None, legacy_connected=True)
    timers.fire("_finish_bearer_settle")
    for _ in range(4):
        timers.fire("_retry_subscribe")
    assert timers.delays("_retry_subscribe") == [32]

    gatt.connect_le()
    client.observe_bearer_state(None, legacy_connected=True)  # ServicesResolved

    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
    timers.fire("_retry_subscribe")
    _answer_probe(client)
    assert client.connected


def test_silent_le_loss_is_found_by_the_health_probe(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    _connect(client)

    gatt.drop_le()  # Device1.Connected stays true through Classic.
    timers.fire("_check_legacy_health")

    assert not client.connected and client.statuses == [True, False]
    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
    assert timers.delays("_check_legacy_health") == []
    assert gatt.stops == [] and client.resets == []

    gatt.connect_le()
    timers.fire("_retry_subscribe")
    _answer_probe(client)
    assert client.connected and client.statuses == [True, False, True]


def test_aggregate_change_checks_a_proven_transport_immediately(legacy):
    client, gatt = legacy, legacy.gatt
    _connect(client)
    writes = gatt.writes

    client.observe_bearer_state(None, legacy_connected=True)
    assert gatt.writes == writes + 1  # LE is still there: one health probe.
    _answer_probe(client)
    assert client.connected

    gatt.drop_le()
    client.observe_bearer_state(None, legacy_connected=True)
    assert not client.connected


def test_healthy_transport_keeps_being_checked(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    _connect(client)

    for _ in range(3):
        writes = gatt.writes
        timers.fire("_check_legacy_health")
        assert gatt.writes == writes + 1
        _answer_probe(client)
        assert client.connected
        assert timers.delays("_check_legacy_health") == [
            client_module.LEGACY_HEALTH_SECONDS
        ]
    assert client.statuses == [True]


def test_unanswered_probe_on_a_proven_transport_leaves_the_registrations(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    _connect(client)

    timers.fire("_check_legacy_health")
    timers.fire("_request_timed_out")  # The write went out; no reply came.

    # ATT cannot be known to be settled here, so nothing is stopped. The
    # transport is treated as lost and probed again with backoff.
    assert gatt.stops == [] and client.resets == []
    assert not client.connected and client.statuses == [True, False]
    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
    timers.fire("_retry_subscribe")
    _answer_probe(client)
    assert client.connected


def test_pending_permission_is_retried_promptly_not_backed_off(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    gatt.connect_le()
    client.observe_bearer_state(None, legacy_connected=True)
    timers.fire("_finish_bearer_settle")

    timers.fire("_request_timed_out")  # iOS has not granted access yet.

    assert client.subscribed and gatt.stops == []
    assert timers.delays("_retry_authorization") == [
        client_module.AUTHORIZATION_RETRY_SECONDS
    ]
    timers.fire("_retry_authorization")
    _answer_probe(client)
    assert client.connected


def test_device_disconnect_ends_probing(legacy):
    client, timers = legacy, legacy.timers
    _connect(client)

    client.observe_bearer_state(False, legacy_connected=False)

    assert not client.connected and client.statuses == [True, False]
    assert timers.pending == {}

    client.observe_bearer_state(None, legacy_connected=True)
    assert timers.delays("_finish_bearer_settle") == [
        client_module.BEARER_SETTLE_SECONDS
    ]


def test_returning_characteristics_resume_probing(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    client.observe_bearer_state(None, legacy_connected=True)
    timers.fire("_finish_bearer_settle")
    assert timers.delays("_retry_subscribe") == [2]

    for _ in range(4):
        timers.fire("_retry_subscribe")
    assert timers.delays("_retry_subscribe") == [32]

    client._on_iface_removed(CP, ["org.bluez.GattCharacteristic1"])
    assert timers.delays("_retry_subscribe") == []

    client._on_iface_added(CP, {"org.bluez.GattCharacteristic1": {
        "UUID": client_module.CONTROL_POINT_CHAR,
    }})
    # Their return is as good a sign of LE as any: do not wait out backoff.
    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
    gatt.connect_le()
    timers.fire("_retry_subscribe")
    _answer_probe(client)
    assert client.connected


def test_a_monitored_bearer_still_fails_on_a_pending_registration(legacy):
    client, gatt, timers = legacy, legacy.gatt, legacy.timers
    gatt.registered = {NS}  # Left pending by an earlier attempt.
    gatt.le_up = True

    client.observe_bearer_state(True)
    timers.fire("_finish_bearer_settle")

    # InProgress is tolerated only when LE cannot be observed.
    assert not client.subscribed
    assert timers.delays("_retry_subscribe") == [client_module.SUBSCRIBE_RETRY_SECONDS]
