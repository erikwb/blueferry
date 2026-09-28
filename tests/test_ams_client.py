"""AMS GATT orchestration against an inert, manually completed fake BlueZ."""
from __future__ import annotations

import dbus
import pytest

from blueferry.ams.client import AmsClient, AmsUnavailableError
from blueferry.ams.constants import (
    ENTITY_ATTRIBUTE_CHAR,
    ENTITY_UPDATE_CHAR,
    REMOTE_COMMAND_CHAR,
    EntityID,
    RemoteCommandID,
    TrackAttributeID,
)
from blueferry.ams.parsers import EntityUpdate
from blueferry.limits import MAX_AMS_PENDING_OPERATIONS

DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
RC = f"{DEVICE}/service0040/char0041"
EU = f"{DEVICE}/service0040/char0044"
EA = f"{DEVICE}/service0040/char0047"
GATT = "org.bluez.GattCharacteristic1"


class _Match:
    def __init__(self, bus, handler, path) -> None:
        self.bus = bus
        self.handler = handler
        self.path = path
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _Call:
    def __init__(self, path, method, args, reply, error) -> None:
        self.path = path
        self.method = method
        self.args = args
        self.reply = reply
        self.error = error

    def succeed(self, *value) -> None:
        self.reply(*value)

    def fail(self, name="org.bluez.Error.Failed", message="failed") -> None:
        self.error(dbus.exceptions.DBusException(message, name=name))


class _Object:
    def __init__(self, bus, path) -> None:
        self._bus = bus
        self._path = path

    def __getattr__(self, method):
        def call(*args, reply_handler, error_handler, **_kwargs):
            self._bus.calls.append(
                _Call(self._path, method, args, reply_handler, error_handler)
            )
        return call


class _Bus:
    """Records every asynchronous call; tests complete them explicitly."""

    def __init__(self) -> None:
        self.matches: list[_Match] = []
        self.calls: list[_Call] = []

    def add_signal_receiver(self, handler, **kwargs):
        match = _Match(self, handler, kwargs.get("path"))
        self.matches.append(match)
        return match

    def get_object(self, _name, path, introspect=True):
        assert introspect is False
        return _Object(self, path)

    def take(self, method, path=None) -> _Call:
        for index, call in enumerate(self.calls):
            if call.method == method and (path is None or call.path == path):
                return self.calls.pop(index)
        raise AssertionError(f"no pending {method} on {path}: {[c.method for c in self.calls]}")

    def pending(self) -> list[tuple[str, str]]:
        return [(call.method, call.path) for call in self.calls]

    def notify(self, path, value: bytes) -> None:
        for match in self.matches:
            if match.path == path and not match.removed:
                match.handler(GATT, {"Value": dbus.Array(list(value))}, [])


class _Timers:
    def __init__(self) -> None:
        self.pending: dict[int, object] = {}
        self.delays: dict[int, int] = {}
        self._next = 0

    def schedule(self, delay, callback) -> int:
        self._next += 1
        self.pending[self._next] = callback
        self.delays[self._next] = delay
        return self._next

    def cancel(self, source) -> None:
        self.pending.pop(source, None)

    def run_all(self) -> None:
        for source, callback in list(self.pending.items()):
            self.pending.pop(source, None)
            callback()


def _objects():
    return {
        path: {GATT: {"UUID": uuid.upper()}}
        for path, uuid in (
            (RC, REMOTE_COMMAND_CHAR), (EU, ENTITY_UPDATE_CHAR), (EA, ENTITY_ATTRIBUTE_CHAR),
        )
    }


@pytest.fixture
def harness():
    bus = _Bus()
    timers = _Timers()
    updates: list[EntityUpdate] = []
    commands: list[frozenset] = []
    availability: list[bool] = []
    client = AmsClient(
        DEVICE,
        on_update=updates.append,
        on_supported_commands=commands.append,
        on_availability=availability.append,
        bus_factory=lambda: bus,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    return client, bus, timers, updates, commands, availability


def _bytes(call: _Call) -> bytes:
    return bytes(int(value) for value in call.args[0])


def _subscribe(client, bus, timers) -> None:
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())
    timers.run_all()  # bearer settle
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for expected in (b"\x00\x00\x01\x02", b"\x01\x00\x01\x02\x03", b"\x02\x00\x01\x02\x03"):
        call = bus.take("WriteValue", EU)
        assert _bytes(call) == expected
        call.succeed()


def test_subscribes_and_registers_all_entities_after_bearer_settles(harness) -> None:
    client, bus, timers, _updates, _commands, availability = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())

    # Characteristics exist, but nothing is written before the LE link settles.
    assert bus.pending() == []
    timers.run_all()
    assert bus.pending() == [("StartNotify", RC)]
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        assert not client.available
        bus.take("WriteValue", EU).succeed()

    assert client.available
    assert availability == [True]
    # Entity Attribute is only read on demand; no subscription for it.
    assert all(match.path != EA for match in bus.matches)


def test_ignores_characteristics_of_other_devices(harness) -> None:
    client, bus, timers, *_ = harness
    other = {
        path.replace("dev_AA_BB_CC_DD_EE_FF", "dev_11_22_33_44_55_66"): value
        for path, value in _objects().items()
    }
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(other)
    timers.run_all()
    assert bus.pending() == []
    assert not client.characteristics_found


def test_updates_and_supported_commands_are_delivered(harness) -> None:
    client, bus, timers, updates, commands, _ = harness
    _subscribe(client, bus, timers)

    bus.notify(RC, bytes([0, 1, 3, 99]))
    bus.notify(EU, bytes([2, 2, 0]) + b"Song")
    bus.notify(EU, b"\x02")  # malformed: dropped without raising

    assert commands == [frozenset({
        RemoteCommandID.Play, RemoteCommandID.Pause, RemoteCommandID.NextTrack,
    })]
    assert updates == [EntityUpdate(EntityID.Track, TrackAttributeID.Title, False, "Song")]
    assert bus.pending() == []


def test_truncated_value_is_fetched_once_through_entity_attribute(harness) -> None:
    client, bus, timers, updates, *_ = harness
    _subscribe(client, bus, timers)

    bus.notify(EU, bytes([2, 2, 1]) + b"Symphony No")
    bus.notify(EU, bytes([2, 2, 1]) + b"Symphony No")  # duplicate while pending
    selector = bus.take("WriteValue", EA)
    assert _bytes(selector) == bytes([EntityID.Track, TrackAttributeID.Title])
    assert bus.pending() == []
    selector.succeed()
    bus.take("ReadValue", EA).succeed(dbus.Array(list(b"Symphony No. 9 in D minor")))

    assert [update.value for update in updates] == [
        "Symphony No", "Symphony No", "Symphony No. 9 in D minor",
    ]
    assert updates[-1].truncated is False
    # A later truncation of the same attribute is fetched again.
    bus.notify(EU, bytes([2, 2, 1]) + b"Next")
    assert _bytes(bus.take("WriteValue", EA)) == b"\x02\x02"


def test_failed_full_read_keeps_the_truncated_value(harness) -> None:
    client, bus, timers, updates, *_ = harness
    _subscribe(client, bus, timers)
    bus.notify(EU, bytes([2, 0, 1]) + b"Art")
    bus.take("WriteValue", EA).succeed()
    bus.take("ReadValue", EA).fail(name="org.bluez.Error.Failed", message="0xa2")
    assert [update.value for update in updates] == ["Art"]
    assert client.available


def test_command_is_written_as_one_byte_and_reports_completion(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    done = []

    client.send_command(RemoteCommandID.NextTrack, lambda: done.append("ok"), done.append)
    call = bus.take("WriteValue", RC)
    assert _bytes(call) == b"\x03"
    call.succeed()
    assert done == ["ok"]

    client.send_command(RemoteCommandID.Pause, lambda: done.append("ok"), done.append)
    bus.take("WriteValue", RC).fail(message="Application error 0xa0")
    assert isinstance(done[-1], dbus.exceptions.DBusException)


def test_commands_are_serialized_behind_a_pending_attribute_read(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    bus.notify(EU, bytes([2, 2, 1]) + b"Tit")
    client.send_command(RemoteCommandID.Play, lambda: None, lambda _error: None)

    assert bus.pending() == [("WriteValue", EA)]
    bus.take("WriteValue", EA).succeed()
    # The command must not slip in between the selector write and its read.
    assert bus.pending() == [("ReadValue", EA)]
    bus.take("ReadValue", EA).succeed(dbus.Array(list(b"Title")))
    assert _bytes(bus.take("WriteValue", RC)) == b"\x00"


def test_command_before_subscription_fails_without_bus_traffic(harness) -> None:
    client, bus, *_ = harness
    errors = []
    client.send_command(RemoteCommandID.Play, lambda: None, errors.append)
    assert isinstance(errors[0], AmsUnavailableError)
    assert bus.pending() == []


def test_bounded_queue_rejects_command_floods(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    errors = []
    for _ in range(MAX_AMS_PENDING_OPERATIONS + 5):
        client.send_command(RemoteCommandID.VolumeUp, lambda: None, errors.append)
    # One write is active; the backlog is bounded.
    assert len(bus.calls) == 1
    # One active write plus a full backlog; everything beyond is refused.
    assert len(errors) == (MAX_AMS_PENDING_OPERATIONS + 5) - (MAX_AMS_PENDING_OPERATIONS + 1)


def test_bearer_loss_resets_and_discards_late_replies(harness) -> None:
    client, bus, timers, updates, _commands, availability = harness
    _subscribe(client, bus, timers)
    bus.notify(EU, bytes([2, 2, 1]) + b"Old")
    stale_selector = bus.take("WriteValue", EA)
    failures = []
    client.send_command(RemoteCommandID.Play, lambda: None, failures.append)

    client.observe_bearer_state(False)

    assert not client.available
    assert availability == [True, False]
    assert len(failures) == 1 and isinstance(failures[0], AmsUnavailableError)
    assert all(match.removed for match in bus.matches if match.path in (RC, EU))
    # No StopNotify on a dropped link (bluetoothd 5.87 crash, see PROTOCOL.md).
    assert "StopNotify" not in [call.method for call in bus.calls]
    stale_selector.succeed()
    assert bus.pending() == []
    bus.notify(EU, bytes([2, 2, 0]) + b"Ghost")
    assert [update.value for update in updates] == ["Old"]


def test_reconnect_reregisters_and_keeps_surviving_ccc_registration(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    client.observe_bearer_state(False)
    client.observe_bearer_state(True)
    timers.run_all()

    # BlueZ still reports Notifying=true for our registrations.
    bus.take("Get", RC).succeed(True)
    bus.take("Get", EU).succeed(True)
    assert "StartNotify" not in [call.method for call in bus.calls]
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available


def test_reconnect_restarts_notifications_when_bluez_dropped_them(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    client.observe_bearer_state(False)
    client.observe_bearer_state(True)
    timers.run_all()
    bus.take("Get", RC).succeed(False)
    bus.take("StartNotify", RC).succeed()
    bus.take("Get", EU).fail()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available


def test_subscription_failure_retries_with_backoff(harness) -> None:
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())
    timers.run_all()
    bus.take("StartNotify", RC).fail(name="org.bluez.Error.InProgress")

    assert not client.available
    assert list(timers.delays.values())[-1] == 2
    timers.run_all()
    bus.take("StartNotify", RC).fail(name="org.bluez.Error.NotConnected")
    assert list(timers.delays.values())[-1] == 4
    timers.run_all()
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available


def test_characteristic_removal_and_owner_change_reset_state(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    manager_added = next(m for m in bus.matches if m.path == "/")
    removed = [m for m in bus.matches if m.path == "/"][1]

    removed.handler(EA, [GATT])
    assert not client.available and not client.characteristics_found

    manager_added.handler(EA, {GATT: {"UUID": ENTITY_ATTRIBUTE_CHAR}})
    bus.take("Get", RC).succeed(True)
    bus.take("Get", EU).succeed(True)
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available

    client.observe_bluez_owner(":1.1", ":1.2")
    assert not client.available and not client.characteristics_found
    bus.take("GetManagedObjects").succeed(_objects())
    # The old owner's LE observation is gone; wait for the bearer supervisor.
    assert bus.pending() == []
    client.observe_bearer_state(True)
    timers.run_all()
    assert bus.pending() == [("StartNotify", RC)]


def test_stop_is_inert_afterwards(harness) -> None:
    client, bus, timers, updates, *_ = harness
    _subscribe(client, bus, timers)
    client.stop()
    assert all(match.removed for match in bus.matches)
    assert "StopNotify" not in [call.method for call in bus.calls]
    bus.notify(EU, bytes([2, 2, 0]) + b"x")
    assert updates == []
    client.stop()


def test_characteristic_removed_and_added_during_settle_resubscribes(harness) -> None:
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())
    manager_added, manager_removed = [m for m in bus.matches if m.path == "/"]

    # BlueZ re-enumerates GATT while the LE link is still settling.
    manager_removed.handler(EA, [GATT])
    manager_added.handler(EA, {GATT: {"UUID": ENTITY_ATTRIBUTE_CHAR}})
    assert bus.pending() == []
    timers.run_all()

    assert bus.pending() == [("StartNotify", RC)]


def test_bearer_loss_during_settle_cancels_it(harness) -> None:
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())
    client.observe_bearer_state(False)
    assert timers.pending == {}
    assert bus.pending() == []


def test_missing_first_update_resubscribes_with_backoff(harness) -> None:
    client, bus, timers, _updates, _commands, availability = harness
    _subscribe(client, bus, timers)
    assert client.available
    assert list(timers.delays.values())[-1] == 10

    timers.run_all()  # no Entity Update arrived within the window

    assert not client.available
    assert availability == [True, False]
    assert list(timers.delays.values())[-1] == 2  # retry, not an LE reset
    timers.run_all()
    # The cached Notifying flag is not trusted after a silent registration.
    assert bus.pending() == [("StartNotify", RC)]
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    timers.run_all()
    assert list(timers.delays.values())[-1] == 4  # silence keeps backing off


def test_first_update_disarms_the_watchdog(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    bus.notify(EU, bytes([0, 0, 0]) + b"Music")
    assert timers.pending == {}
    assert client.available


@pytest.mark.parametrize("message,expected", [
    ("Operation failed with ATT error: 0xa0", "(AMS InvalidState 0xA0)"),
    ("Application error 0xA1", "(AMS InvalidCommand 0xA1)"),
    ("att error 0xa2", "(AMS AbsentAttribute 0xA2)"),
])
def test_ams_att_codes_are_logged_by_name(harness, caplog, message, expected) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    caplog.set_level("INFO", logger="blueferry.ams.client")
    client.send_command(RemoteCommandID.Play, lambda: None, lambda _error: None)
    bus.take("WriteValue", RC).fail(name="org.bluez.Error.Failed", message=message)
    assert f"org.bluez.Error.Failed {expected}" in caplog.text
    assert message not in caplog.text


def test_unknown_att_code_logs_only_the_error_name(harness, caplog) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    caplog.set_level("INFO", logger="blueferry.ams.client")
    bus.notify(EU, bytes([2, 2, 1]) + b"Tit")
    bus.take("WriteValue", EA).fail(message="secret detail 0x0e")
    assert "org.bluez.Error.Failed" in caplog.text
    assert "secret detail" not in caplog.text
    assert "AMS " not in caplog.text.split("full attribute read failed")[-1]


def test_subscription_failure_names_the_ams_code(harness, caplog) -> None:
    client, bus, timers, *_ = harness
    caplog.set_level("INFO", logger="blueferry.ams.client")
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())
    timers.run_all()
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    bus.take("WriteValue", EU).fail(message="ATT error: 0xa1")
    assert "AMS InvalidCommand 0xA1" in caplog.text
