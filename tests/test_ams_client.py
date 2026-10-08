"""AMS GATT orchestration against an inert, manually completed fake BlueZ."""
from __future__ import annotations

import dbus
import pytest

from blueferry.ams.client import AmsClient, AmsNotifySessions, AmsUnavailableError
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


class _SessionBus(_Bus):
    """A BlueZ and iPhone that keep notify sessions the way the real ones do.

    BlueZ holds one session per sender and characteristic: a repeated
    StartNotify answers without writing the CCC, a session survives an LE
    drop, and BlueZ re-enables its CCC at link-up. iOS answers a CCC write on
    Remote Command with its command list and an Entity Update registration
    with the current value.
    """

    COMMANDS = bytes([0, 1, 2, 3, 4])
    TITLE = bytes([2, 2, 0]) + b"Song"

    def __init__(self) -> None:
        super().__init__()
        self.sessions: set[str] = set()
        self.log: list[tuple[str, str]] = []  # answered GATT calls, in order
        self.hold: set[str] = set()  # methods left pending for the test
        self.failing_stops = 0
        self.link = True

    def pump(self) -> None:
        """Answer every pending call that the test does not hold back."""
        progressed = True
        while progressed:
            progressed = False
            for call in list(self.calls):
                if call.method not in self.hold:
                    self.calls.remove(call)
                    self._answer(call)
                    progressed = True

    def _answer(self, call: _Call) -> None:
        method, path = call.method, call.path
        if method == "GetManagedObjects":
            call.succeed(_objects())
            return
        self.log.append((method, path))
        if method == "StartNotify":
            fresh = path not in self.sessions
            self.sessions.add(path)
            call.succeed()
            if fresh:
                self._ccc_enabled(path)
        elif method == "StopNotify":
            if self.failing_stops:
                self.failing_stops -= 1
                call.fail(name="org.bluez.Error.Failed")
            elif path not in self.sessions:
                call.fail(name="org.bluez.Error.Failed", message="No notify session started")
            else:
                self.sessions.discard(path)
                call.succeed()
        elif method == "Get":
            call.succeed(dbus.Boolean(path in self.sessions))
        else:
            call.succeed()
            if method == "WriteValue" and path == EU and path in self.sessions:
                if _bytes(call)[:1] == bytes([EntityID.Track]):
                    self.notify(EU, self.TITLE)

    def _ccc_enabled(self, path) -> None:
        if path == RC and self.link:
            self.notify(RC, self.COMMANDS)

    def link_down(self) -> None:
        self.link = False

    def link_up(self) -> None:
        self.link = True
        for path in sorted(self.sessions):
            self._ccc_enabled(path)

    def stops(self) -> list[str]:
        return [path for method, path in self.log if method == "StopNotify"]


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
    # The value receivers belong to the characteristic objects, which BlueZ
    # keeps across an LE drop; see the reconnect tests below.
    assert not any(match.removed for match in bus.matches if match.path in (RC, EU))
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


def test_runtime_opt_out_releases_the_notifications_on_a_steady_link(harness) -> None:
    """Review #207: iOS resends its command list only on a CCC write."""
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    released = []

    client.stop(release=True, on_released=lambda: released.append(True))

    assert sorted(bus.pending()) == sorted([("StopNotify", RC), ("StopNotify", EU)])
    bus.take("StopNotify", RC).succeed()
    assert released == []
    bus.take("StopNotify", EU).succeed()
    assert released == [True]
    assert timers.pending == {}


def test_a_failed_stop_notify_is_not_a_release_and_is_retried(session_harness) -> None:
    """Review #207: the session survived, so the next opt-in got no command list."""
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    bus.failing_stops = 1
    released = []

    client.stop(release=True, on_released=lambda: released.append(True))
    bus.pump()

    # Remote Command failed, Entity Update was stopped.
    assert bus.sessions == {RC} and released == [] and sessions.release_owed
    assert _settle_delays(timers) == [2]
    timers.run_all()
    assert bus.pending() == [("StopNotify", RC)]
    bus.pump()
    assert bus.sessions == set() and released == [True]
    assert not sessions.release_owed and timers.pending == {}

    second, commands = new_client()
    second.observe_bearer_state(True)
    second.start()
    _run(bus, timers)
    assert second.available and len(commands[-1]) == 5


def test_a_stop_notify_that_keeps_failing_is_retried_a_bounded_number_of_times(
    session_harness,
) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    bus.failing_stops = 1000

    client.stop(release=True)
    delays = []
    for _attempt in range(10):
        bus.pump()
        delays += _settle_delays(timers)
        timers.run_all()

    # Three attempts, then the release rests: no timer, no tight loop.
    assert len(bus.stops()) == 3 * 2 and delays == [2, 4]
    assert sessions.release_owed and bus.pending() == [] and timers.pending == {}

    # The next settled link is the next safe moment.
    sessions.observe_bearer_state(False)
    sessions.observe_bearer_state(True)
    assert _settle_delays(timers) == [3]
    bus.failing_stops = 0
    timers.run_all()
    bus.pump()
    assert bus.sessions == set() and not sessions.release_owed


def test_an_opt_in_does_not_wait_forever_for_a_failing_release(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    bus.failing_stops = 1000
    client.stop(release=True)
    for _attempt in range(10):
        bus.pump()
        timers.run_all()
    bus.log.clear()
    released = []

    sessions.when_released(lambda: released.append(True))
    assert released == []
    for _attempt in range(10):
        bus.pump()
        timers.run_all()

    # A new round of attempts, then the opt-in goes ahead on the old sessions.
    assert len(bus.stops()) == 3 * 2
    assert released == [True] and not sessions.release_owed
    assert bus.pending() == [] and timers.pending == {}
    # The sessions stay marked, so the next opt-out tries again.
    bus.failing_stops = 0
    sessions.release()
    bus.pump()
    assert bus.sessions == set()


@pytest.mark.parametrize("error", [
    {"name": "org.freedesktop.DBus.Error.UnknownObject"},
    {"name": "org.bluez.Error.Failed", "message": "No notify session started"},
])
def test_a_stop_notify_refused_for_a_missing_session_counts_as_released(
    session_harness, error,
) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)

    client.stop(release=True)
    bus.take("StopNotify", RC).fail(**error)
    bus.take("StopNotify", EU).fail(**error)

    assert not sessions.release_owed and timers.pending == {}


def test_a_link_drop_cancels_the_release_retry(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    bus.failing_stops = 2
    client.stop(release=True)
    bus.pump()
    assert _settle_delays(timers) == [2]

    sessions.observe_bearer_state(False)

    assert timers.pending == {} and bus.pending() == []
    assert sessions.release_owed


@pytest.fixture
def session_harness():
    """One session tracker and bus shared by successive clients, as in the daemon."""
    bus = _SessionBus()
    timers = _Timers()
    sessions = AmsNotifySessions(
        bus_factory=lambda: bus, schedule=timers.schedule, cancel=timers.cancel,
    )

    def new_client():
        commands: list[frozenset] = []
        client = AmsClient(
            DEVICE, on_update=lambda _update: None,
            on_supported_commands=commands.append, sessions=sessions,
            bus_factory=lambda: bus, schedule=timers.schedule, cancel=timers.cancel,
        )
        return client, commands

    return new_client, sessions, bus, timers


def _run(bus, timers) -> None:
    bus.pump()
    timers.run_all()
    bus.pump()


def _settle_delays(timers) -> list[int]:
    return [timers.delays[source] for source in timers.pending]


def test_opt_out_while_the_link_is_down_releases_after_it_settles_again(session_harness) -> None:
    """Review #207: BlueZ re-enables a surviving CCC at link-up by itself."""
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    assert client.available
    client.observe_bearer_state(False)
    bus.link_down()
    released = []

    client.stop(release=True, on_released=lambda: released.append(True))

    # No StopNotify on a dropped link (bluetoothd 5.87 crash, see PROTOCOL.md).
    assert bus.pending() == [] and timers.pending == {}
    assert released == [] and sessions.release_owed

    bus.link_up()
    sessions.observe_bearer_state(True)
    # BlueZ is re-registering notifications: wait out the settle window.
    assert bus.pending() == []
    assert _settle_delays(timers) == [3]
    timers.run_all()
    assert sorted(bus.pending()) == sorted([("StopNotify", RC), ("StopNotify", EU)])
    bus.pump()
    assert released == [True] and not sessions.release_owed
    assert bus.sessions == set() and timers.pending == {}


def test_a_link_drop_inside_the_settle_window_restarts_the_wait(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    client.observe_bearer_state(False)
    client.stop(release=True)

    sessions.observe_bearer_state(True)
    sessions.observe_bearer_state(False)
    assert timers.pending == {}
    sessions.observe_bearer_state(True)
    assert _settle_delays(timers) == [3] and bus.pending() == []
    timers.run_all()
    assert len(bus.pending()) == 2


def test_opt_out_inside_the_settle_window_waits_for_its_end(session_harness) -> None:
    """Review #207: StopNotify while BlueZ re-registers crashes bluetoothd 5.87."""
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    client.observe_bearer_state(False)
    bus.link_down()
    bus.link_up()
    client.observe_bearer_state(True)

    client.stop(release=True)

    assert bus.pending() == []
    assert _settle_delays(timers) == [3]
    timers.run_all()
    bus.pump()
    assert sorted(bus.stops()) == sorted([RC, EU])
    assert not sessions.release_owed


@pytest.mark.parametrize("answer", ["succeed", "fail"])
def test_opt_out_with_start_notify_pending_releases_after_its_reply(
    session_harness, answer,
) -> None:
    """Review #207: a StartNotify answered after stop() still leaves a session."""
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    bus.pump()
    bus.hold = {"StartNotify"}
    timers.run_all()
    assert bus.pending() == [("StartNotify", RC)]
    released = []

    client.stop(release=True, on_released=lambda: released.append(True))

    assert bus.pending() == [("StartNotify", RC)]
    assert released == [] and sessions.release_owed
    if answer == "succeed":
        bus.hold = set()
        bus.pump()
        assert bus.log == [("StartNotify", RC), ("StopNotify", RC)]
    else:
        # A refused StartNotify created no session; nothing is left to stop.
        bus.take("StartNotify", RC).fail()
        assert bus.pending() == []
    assert released == [True] and not sessions.release_owed
    assert bus.sessions == set()


def test_an_unanswered_start_notify_is_released_as_possibly_started(session_harness) -> None:
    new_client, _sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    bus.pump()
    timers.run_all()
    client.stop(release=True)

    bus.take("StartNotify", RC).fail(name="org.freedesktop.DBus.Error.NoReply")

    assert bus.pending() == [("StopNotify", RC)]


def test_opt_out_with_registrations_pending_releases_both_sessions(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    bus.pump()
    bus.hold = {"WriteValue"}
    timers.run_all()
    bus.pump()
    assert bus.sessions == {RC, EU} and not client.available

    client.stop(release=True)
    bus.hold = set()
    bus.pump()

    assert sorted(bus.stops()) == sorted([RC, EU])
    assert bus.sessions == set() and not sessions.release_owed


def test_a_new_client_after_the_release_gets_the_command_list_again(session_harness) -> None:
    new_client, _sessions, bus, timers = session_harness
    first, commands = new_client()
    first.observe_bearer_state(True)
    first.start()
    _run(bus, timers)
    assert len(commands[-1]) == 5
    first.stop(release=True)
    bus.pump()

    second, commands = new_client()
    second.observe_bearer_state(True)
    second.start()
    _run(bus, timers)

    assert second.available and len(commands[-1]) == 5


def test_removed_characteristics_and_a_new_bluetoothd_owe_nothing(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    manager_removed = [match for match in bus.matches if match.path == "/"][1]
    manager_removed.handler(RC, [GATT])
    client.observe_bearer_state(False)
    client.stop(release=True)
    assert sessions.release_owed
    released = []
    sessions.when_released(lambda: released.append(True))

    # The remaining session belonged to the bluetoothd that went away.
    sessions.observe_bluez_owner(":1.1", "")

    assert released == [True] and not sessions.release_owed
    sessions.observe_bearer_state(True)
    assert bus.pending() == [] and timers.pending == {}


def test_closing_the_tracker_drops_an_owed_release(session_harness) -> None:
    new_client, sessions, bus, timers = session_harness
    client, _commands = new_client()
    client.observe_bearer_state(True)
    client.start()
    _run(bus, timers)
    bus.hold = {"StopNotify"}
    released = []
    client.stop(release=True, on_released=lambda: released.append(True))

    sessions.close()
    bus.hold = set()
    bus.pump()

    assert released == [] and timers.pending == {}


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
    assert availability == [True, False, True]
    timers.run_all()
    # One resubscribe per link: a second silent registration means an idle
    # Media Source, so availability does not flap and nothing is retried.
    assert client.available
    assert availability == [True, False, True]
    assert timers.pending == {}
    assert bus.pending() == []


def test_a_new_link_restores_the_silence_budget(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    timers.run_all()  # silent: resubscribe once
    timers.run_all()
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    timers.run_all()  # silent again: budget spent
    assert client.available

    client.observe_bearer_state(False)
    client.observe_bearer_state(True)
    timers.run_all()
    bus.take("Get", RC).succeed(True)
    bus.take("Get", EU).succeed(True)
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    timers.run_all()  # the new link gets one resubscribe again
    assert not client.available


def test_supported_command_list_also_disarms_the_watchdog(harness) -> None:
    client, bus, timers, *_ = harness
    _subscribe(client, bus, timers)
    bus.notify(RC, bytes([0, 1]))
    assert timers.pending == {}
    assert client.available


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


def test_commands_survive_a_reconnect_when_bluez_rewrites_the_ccc(harness) -> None:
    """Review #207: iOS sends its command list while the link settles."""
    client, bus, timers, _updates, commands, _availability = harness
    _subscribe(client, bus, timers)
    bus.notify(RC, bytes([0, 1, 3]))
    client.observe_bearer_state(False)
    assert not client.available

    client.observe_bearer_state(True)
    # BlueZ re-enabled the surviving CCC at link-up; iOS answers at once,
    # before BlueFerry's settle timer has fired.
    bus.notify(RC, bytes([0, 1]))
    assert commands[-1] == frozenset({RemoteCommandID.Play, RemoteCommandID.Pause})
    timers.run_all()
    bus.take("Get", RC).succeed(True)
    bus.take("Get", EU).succeed(True)
    assert "StartNotify" not in [call.method for call in bus.calls]
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available

    done = []
    client.send_command(RemoteCommandID.Play, lambda: done.append("ok"), done.append)
    bus.take("WriteValue", RC).succeed()
    assert done == ["ok"]
    # A soft reset never reported an empty list.
    assert frozenset() not in commands


def test_entity_updates_without_a_live_link_are_ignored(harness) -> None:
    client, bus, timers, updates, *_ = harness
    _subscribe(client, bus, timers)
    client.observe_bearer_state(False)
    bus.notify(EU, bytes([2, 2, 0]) + b"Ghost")
    assert updates == []


def test_removing_the_command_characteristic_clears_the_list(harness) -> None:
    client, bus, timers, _updates, commands, _ = harness
    _subscribe(client, bus, timers)
    bus.notify(RC, bytes([0]))
    removed = [m for m in bus.matches if m.path == "/"][1]
    removed.handler(RC, [GATT])
    assert commands[-1] == frozenset()
    assert all(m.removed for m in bus.matches if m.path == RC)


def test_owner_change_clears_the_list_and_receivers(harness) -> None:
    client, bus, timers, _updates, commands, _ = harness
    _subscribe(client, bus, timers)
    bus.notify(RC, bytes([0]))
    client.observe_bluez_owner(":1.1", "")
    assert commands[-1] == frozenset()
    assert all(m.removed for m in bus.matches)


class _FlakyBus(_Bus):
    def __init__(self) -> None:
        super().__init__()
        self.fail_receivers = 0

    def add_signal_receiver(self, handler, **kwargs):
        if self.fail_receivers:
            self.fail_receivers -= 1
            raise dbus.exceptions.DBusException(
                "gone", name="org.freedesktop.DBus.Error.NameHasNoOwner",
            )
        return super().add_signal_receiver(handler, **kwargs)


@pytest.fixture
def flaky():
    bus = _FlakyBus()
    timers = _Timers()
    client = AmsClient(
        DEVICE,
        on_update=lambda _update: None,
        on_supported_commands=lambda _commands: None,
        bus_factory=lambda: bus,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    return client, bus, timers


def test_owner_change_failure_is_contained_and_retried(flaky) -> None:
    """Review #207: an AMS error must not abort bluetoothd-restart recovery."""
    client, bus, timers = flaky
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed({})

    bus.fail_receivers = 1
    client.observe_bluez_owner(":1.1", ":1.2")  # must not raise
    assert list(timers.delays.values())[-1] == 2
    assert bus.pending() == []

    timers.run_all()
    bus.take("GetManagedObjects").succeed(_objects())
    assert client.characteristics_found


def test_start_failure_is_contained_and_retried(flaky) -> None:
    client, bus, timers = flaky
    bus.fail_receivers = 1
    client.start()
    assert bus.pending() == []
    timers.run_all()
    assert bus.pending() == [("GetManagedObjects", "/")]


def test_failed_object_sweep_is_retried_with_backoff(harness) -> None:
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").fail(name="org.freedesktop.DBus.Error.NoReply")
    assert list(timers.delays.values())[-1] == 2
    timers.run_all()
    bus.take("GetManagedObjects").fail(name="org.freedesktop.DBus.Error.NoReply")
    assert list(timers.delays.values())[-1] == 4
    timers.run_all()
    bus.take("GetManagedObjects").succeed(_objects())
    assert client.characteristics_found


def test_a_raising_subscription_step_does_not_stick(harness, monkeypatch) -> None:
    """Review #207: _subscribing must not stay set when a bus call raises."""
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())

    def broken(_path):
        raise dbus.exceptions.DBusException("no bus", name="org.freedesktop.DBus.Error.Disconnected")

    original = client._characteristic
    monkeypatch.setattr(client, "_characteristic", broken)
    timers.run_all()  # settle -> subscription attempt fails inside StartNotify
    assert not client.available
    assert list(timers.delays.values())[-1] == 2

    monkeypatch.setattr(client, "_characteristic", original)
    timers.run_all()
    bus.take("StartNotify", RC).succeed()
    bus.take("StartNotify", EU).succeed()
    for _entity in EntityID:
        bus.take("WriteValue", EU).succeed()
    assert client.available


def test_a_raising_subscription_setup_does_not_stick(harness, monkeypatch) -> None:
    client, bus, timers, *_ = harness
    client.observe_bearer_state(True)
    client.start()
    bus.take("GetManagedObjects").succeed(_objects())

    def broken():
        raise RuntimeError("boom")

    original = client._run_subscription
    monkeypatch.setattr(client, "_run_subscription", broken)
    timers.run_all()
    assert client._subscribing is False
    assert list(timers.delays.values())[-1] == 2
    monkeypatch.setattr(client, "_run_subscription", original)
    timers.run_all()
    assert bus.pending() == [("StartNotify", RC)]
