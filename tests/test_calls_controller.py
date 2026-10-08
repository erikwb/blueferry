"""oFono call controller behavior against an inert transport.

No test here reaches oFono, BlueZ, or any real bus: the controller receives a
recording fake transport and a manual timer queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import dbus
import pytest

from blueferry.calls import controller as controller_mod
from blueferry.calls.controller import CallController
from blueferry.calls.model import (
    CALLS_CONNECTING,
    CALLS_DISABLED,
    CALLS_READY,
    CALLS_SEARCHING,
    CALLS_UNAVAILABLE,
    MANAGER_IFACE,
    MODEM_IFACE,
    VOICE_CALL_IFACE,
    VOICE_CALL_MANAGER_IFACE,
)
from blueferry.errors import (
    CallsDisabledError,
    CallsUnavailableError,
    InvalidArgumentsError,
    NotFoundError,
    OperationFailedError,
)

MAC = "AA:BB:CC:DD:EE:FF"
MODEM = "/hfp/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
CALL = f"{MODEM}/voicecall01"


def _missing(name: str = "org.freedesktop.DBus.Error.ServiceUnknown"):
    return dbus.exceptions.DBusException("gone", name=name)


@dataclass
class Match:
    handler: object
    interface: str
    signal: str
    path: str | None
    removed: bool = False

    def remove(self) -> None:
        self.removed = True


@dataclass
class Pending:
    path: str
    interface: str
    method: str
    signature: str
    args: tuple
    on_reply: object
    on_error: object


@dataclass
class FakeTransport:
    pending: list[Pending] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    owner_handler: object = None
    owner_match: Match | None = None
    fail_watch: bool = False
    sent: list = field(default_factory=list)

    def call(self, path, interface, method, signature, args, on_reply, on_error) -> None:
        self.pending.append(Pending(path, interface, method, signature, tuple(args), on_reply, on_error))

    def send(self, path, interface, method, signature, args) -> None:
        self.sent.append((path, interface, method, tuple(args)))

    def watch(self, handler, *, interface, signal, path=None):
        if self.fail_watch:
            raise dbus.exceptions.DBusException("no bus", name="org.freedesktop.DBus.Error.NoServer")
        match = Match(handler, interface, signal, path)
        self.matches.append(match)
        return match

    def watch_owner(self, handler):
        if self.fail_watch:
            raise dbus.exceptions.DBusException("no bus", name="org.freedesktop.DBus.Error.NoServer")
        self.owner_handler = handler
        self.owner_match = Match(handler, "org.freedesktop.DBus", "NameOwnerChanged", None)
        self.matches.append(self.owner_match)
        return self.owner_match

    def emit(self, interface, signal, path, *args) -> None:
        for match in list(self.matches):
            if (not match.removed and match.interface == interface
                    and match.signal == signal and match.path == path):
                match.handler(*args)

    def take(self, method: str) -> Pending:
        for index, pending in enumerate(self.pending):
            if pending.method == method:
                return self.pending.pop(index)
        raise AssertionError(f"no pending {method}; have {[p.method for p in self.pending]}")

    def methods(self) -> list[tuple[str, str, tuple]]:
        return [(p.method, p.path, p.args) for p in self.pending]


class Timers:
    def __init__(self) -> None:
        self.entries: dict[int, tuple[int, object]] = {}
        self._next = 1

    def schedule(self, delay, callback) -> int:
        source = self._next
        self._next += 1
        self.entries[source] = (delay, callback)
        return source

    def cancel(self, source) -> None:
        self.entries.pop(source, None)

    def delays(self) -> list[int]:
        return [delay for delay, _ in self.entries.values()]

    def fire_all(self) -> None:
        for source, (_delay, callback) in list(self.entries.items()):
            self.entries.pop(source, None)
            callback()


def _modem(powered=False, online=False, interfaces=()):
    return (dbus.ObjectPath(MODEM), dbus.Dictionary({
        "Type": dbus.String("hfp"),
        "Powered": dbus.Boolean(powered),
        "Online": dbus.Boolean(online),
        "Interfaces": dbus.Array(list(interfaces), signature="s"),
    }, signature="sv"))


def _build(*, enabled=True, reachable=lambda: True, contacts=None):
    transport = FakeTransport()
    timers = Timers()
    changes: list[str] = []
    events = []
    controller = CallController(
        enabled=enabled,
        mac=MAC,
        adapter="hci0",
        transport=transport,
        resolve_contact=(contacts or {}).get,
        on_calls_changed=lambda: changes.append("calls"),
        on_state_changed=lambda: changes.append("state"),
        on_event=events.append,
        phone_reachable=reachable,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    return controller, transport, timers, changes, events


def _ready(**kwargs):
    built = _build(**kwargs)
    controller, transport, *_ = built
    controller.start()
    transport.take("GetModems").on_reply([_modem(True, True, [VOICE_CALL_MANAGER_IFACE])])
    transport.take("GetCalls").on_reply([])
    assert controller.state == CALLS_READY
    transport.pending.clear()
    return built


def _ring(transport, state="incoming", number="+41 79 123 45 67", path=CALL):
    transport.emit(
        VOICE_CALL_MANAGER_IFACE, "CallAdded", MODEM,
        dbus.ObjectPath(path),
        dbus.Dictionary({
            "State": dbus.String(state),
            "LineIdentification": dbus.String(number),
        }, signature="sv"),
    )


def _noop(*_args) -> None:
    return None


def test_disabled_feature_never_touches_ofono() -> None:
    controller, transport, timers, _changes, _events = _build(enabled=False)

    controller.start()

    assert controller.state == CALLS_DISABLED
    assert controller.snapshot() == {"calls_enabled": False}
    assert transport.pending == [] and transport.matches == [] and timers.entries == {}
    with pytest.raises(CallsDisabledError):
        controller.list_calls()
    with pytest.raises(CallsDisabledError):
        controller.dial("0441234567", _noop, _noop)
    with pytest.raises(CallsDisabledError):
        controller.hangup_all(_noop, _noop)


def test_missing_ofono_reports_unavailable_and_retries_slowly() -> None:
    controller, transport, timers, _changes, _events = _build()

    controller.start()
    transport.take("GetModems").on_error(_missing())

    assert controller.state == CALLS_UNAVAILABLE
    assert timers.delays() == [60]
    assert controller.list_calls() == {"state": "unavailable", "calls": []}
    with pytest.raises(CallsUnavailableError, match="oFono is not running"):
        controller.dial("0441234567", _noop, _noop)

    # oFono starting is noticed immediately through its owner watch.
    transport.owner_handler(True)
    assert controller.state == CALLS_SEARCHING
    assert [p.method for p in transport.pending] == ["GetModems"]


def test_missing_system_bus_keeps_the_daemon_running() -> None:
    controller, transport, timers, _changes, _events = _build()
    transport.fail_watch = True

    controller.start()

    assert controller.state == CALLS_UNAVAILABLE
    assert timers.delays() == [60]
    transport.fail_watch = False
    timers.fire_all()
    assert [p.method for p in transport.pending] == ["GetModems"]


def test_absent_modem_backs_off_then_polls_steadily() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    seen = []
    for _ in range(7):
        transport.take("GetModems").on_reply([])
        seen.extend(timers.delays())
        timers.fire_all()

    assert controller.state == CALLS_SEARCHING
    assert seen == [1, 2, 4, 8, 15, 30, 30]


def test_bring_up_sets_powered_then_online_then_binds() -> None:
    controller, transport, _timers, changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])

    assert controller.state == CALLS_CONNECTING
    powered = transport.take("SetProperty")
    assert (powered.path, powered.interface, powered.args) == (MODEM, MODEM_IFACE, ("Powered", True))
    assert isinstance(powered.args[1], dbus.Boolean)
    # Online is rejected by oFono before Powered, so it must not be sent yet.
    powered.on_reply()
    assert transport.pending == []

    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))
    online = transport.take("SetProperty")
    assert online.args == ("Online", True)
    online.on_reply()
    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Online", dbus.Boolean(True))
    assert controller.state == CALLS_CONNECTING  # VoiceCallManager not announced yet

    transport.emit(
        MODEM_IFACE, "PropertyChanged", MODEM, "Interfaces",
        dbus.Array([VOICE_CALL_MANAGER_IFACE], signature="s"),
    )
    assert controller.state == CALLS_READY
    assert controller.snapshot()["calls_available"] is True
    methods = transport.methods()
    assert ("GetCalls", MODEM, ()) in methods
    volume = [p for p in transport.pending if p.method == "SetProperty"]
    assert sorted(p.args[0] for p in volume) == ["MicrophoneVolume", "SpeakerVolume"]
    assert all(p.args[1] == 100 and isinstance(p.args[1], dbus.Byte) for p in volume)
    assert "state" in changes


def test_volume_failure_is_best_effort() -> None:
    controller, transport, *_ = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem(True, True, [VOICE_CALL_MANAGER_IFACE])])
    for pending in [p for p in transport.pending if p.method == "SetProperty"]:
        pending.on_error(_missing("org.freedesktop.DBus.Error.UnknownInterface"))

    assert controller.state == CALLS_READY


def test_absent_phone_is_not_paged_until_classic_returns() -> None:
    reachable = [False]
    controller, transport, timers, _changes, _events = _build(reachable=lambda: reachable[0])
    controller.start()
    transport.take("GetModems").on_reply([_modem()])

    assert transport.pending == []
    assert timers.delays() == [30]

    reachable[0] = True
    controller.poke()
    assert transport.take("SetProperty").args == ("Powered", True)


def test_rejected_power_request_backs_off() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_error(
        dbus.exceptions.DBusException("x", name="org.ofono.Error.Failed")
    )

    assert timers.delays() == [1]
    timers.fire_all()
    transport.take("GetModems").on_reply([_modem()])
    assert transport.take("SetProperty").args == ("Powered", True)


def test_incoming_call_is_tracked_resolved_and_ended() -> None:
    controller, transport, _timers, changes, events = _ready(
        contacts={"+41791234567": "Alice"},
    )
    changes.clear()

    _ring(transport)

    assert [event.kind for event in events] == ["call_incoming"]
    assert events[0].call.contact_name == "Alice"
    listed = controller.list_calls()
    assert listed["state"] == "ready"
    assert listed["calls"][0]["call_id"] == "voicecall01"
    assert listed["calls"][0]["number"] == "+41791234567"
    assert listed["calls"][0]["direction"] == "incoming"
    assert changes == ["calls"]

    transport.emit(VOICE_CALL_IFACE, "PropertyChanged", CALL, "State", dbus.String("active"))
    assert events[-1].kind == "call_changed"
    assert controller.calls()[0].state == "active"

    transport.emit(VOICE_CALL_MANAGER_IFACE, "CallRemoved", MODEM, dbus.ObjectPath(CALL))
    assert events[-1].kind == "call_ended"
    assert controller.calls() == []
    assert changes == ["calls", "calls", "calls"]


def test_existing_calls_are_loaded_once_on_bind() -> None:
    controller, transport, _timers, _changes, events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem(True, True, [VOICE_CALL_MANAGER_IFACE])])
    _ring(transport, state="active")
    transport.take("GetCalls").on_reply([
        (dbus.ObjectPath(CALL), {"State": "active", "LineIdentification": "+41791234567"}),
    ])

    assert len(controller.calls()) == 1
    assert [event.kind for event in events] == ["call_changed"]


def test_answer_hangup_and_decline_use_the_call_object() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    _ring(transport)
    answered = []

    controller.answer("voicecall01", answered.append, _noop)
    pending = transport.take("Answer")
    assert (pending.path, pending.interface) == (CALL, VOICE_CALL_IFACE)
    pending.on_reply()
    assert answered == [None]

    controller.hangup("voicecall01", _noop, _noop)
    assert transport.take("Hangup").path == CALL

    with pytest.raises(NotFoundError):
        controller.hangup("voicecall99", _noop, _noop)
    with pytest.raises(InvalidArgumentsError):
        controller.hangup("../../org", _noop, _noop)


def test_answering_a_waiting_call_holds_the_active_one() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    _ring(transport, state="active")
    _ring(transport, state="waiting", path=f"{MODEM}/voicecall02")

    controller.answer("voicecall02", _noop, _noop)
    assert transport.take("HoldAndAnswer").path == MODEM

    with pytest.raises(InvalidArgumentsError, match="not ringing"):
        controller.answer("voicecall01", _noop, _noop)


def test_dial_validates_before_ofono_and_returns_the_call_id() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    replies = []

    with pytest.raises(InvalidArgumentsError):
        controller.dial("0800;ATH", replies.append, _noop)
    assert transport.pending == []

    controller.dial("+41 79 123-45-67", replies.append, _noop)
    pending = transport.take("Dial")
    assert (pending.path, pending.interface, pending.args) == (
        MODEM, VOICE_CALL_MANAGER_IFACE, ("+41791234567", ""),
    )
    pending.on_reply(dbus.ObjectPath(f"{MODEM}/voicecall03"))
    assert replies == ["voicecall03"]


def test_dtmf_requires_an_active_call_and_keypad_tones() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    _ring(transport, state="alerting")

    with pytest.raises(InvalidArgumentsError, match="active call"):
        controller.send_tones("voicecall01", "1", _noop, _noop)
    transport.emit(VOICE_CALL_IFACE, "PropertyChanged", CALL, "State", "active")
    with pytest.raises(InvalidArgumentsError):
        controller.send_tones("voicecall01", "1;ATH", _noop, _noop)

    controller.send_tones("voicecall01", "12#", _noop, _noop)
    assert transport.take("SendTones").args == ("12#",)


def test_multi_call_operations_target_the_voice_call_manager() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    with pytest.raises(InvalidArgumentsError):
        controller.hold_and_answer(_noop, _noop)
    _ring(transport, state="waiting")

    controller.swap(_noop, _noop)
    controller.hold_and_answer(_noop, _noop)
    controller.hangup_all(_noop, _noop)

    assert [(p.method, p.path) for p in transport.pending] == [
        ("SwapCalls", MODEM), ("HoldAndAnswer", MODEM), ("HangupAll", MODEM),
    ]


def test_ofono_failure_becomes_a_generic_call_error() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    failures = []

    controller.dial("0441234567", _noop, failures.append)
    transport.take("Dial").on_error(
        dbus.exceptions.DBusException("+41 private detail", name="org.ofono.Error.Failed")
    )

    assert isinstance(failures[0], OperationFailedError)
    assert failures[0].dbus_suffix == "CallFailed"


def test_modem_removal_ends_calls_and_searches_again() -> None:
    controller, transport, timers, _changes, events = _ready()
    _ring(transport)

    transport.emit(MANAGER_IFACE, "ModemRemoved", "/", dbus.ObjectPath(MODEM))

    assert controller.state == CALLS_SEARCHING
    assert controller.calls() == []
    assert events[-1].kind == "call_ended"
    assert timers.delays() == [1]
    with pytest.raises(CallsUnavailableError):
        controller.hangup_all(_noop, _noop)


def test_modem_going_offline_drops_calls_and_repowers() -> None:
    controller, transport, timers, _changes, events = _ready()
    _ring(transport)

    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Online", dbus.Boolean(False))
    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(False))

    assert controller.state == CALLS_CONNECTING
    assert events[-1].kind == "call_ended"
    # Online dropped first (Online requested again), then Powered dropped;
    # the phone is reachable, so the modem is powered up again.
    assert ("SetProperty", MODEM, ("Online", True)) in transport.methods()
    online = transport.take("SetProperty")
    online.on_error(dbus.exceptions.DBusException("x", name="org.ofono.Error.NotAvailable"))
    timers.fire_all()
    transport.take("GetModems").on_reply([_modem()])
    assert transport.take("SetProperty").args == ("Powered", True)


def test_ofono_restart_discards_stale_replies_and_signals() -> None:
    controller, transport, _timers, _changes, events = _ready()
    stale_replies: list = []
    controller.dial("0441234567", stale_replies.append, stale_replies.append)
    stale_dial = transport.take("Dial")
    stale_signals = [match for match in transport.matches if match.signal == "CallAdded"]

    transport.owner_handler(False)
    assert controller.state == CALLS_UNAVAILABLE
    stale_dial.on_reply(dbus.ObjectPath(f"{MODEM}/voicecall09"))
    stale_signals[0].handler(dbus.ObjectPath(CALL), {"State": "incoming"})

    # The caller still gets its answer, but old signals cannot recreate state.
    assert stale_replies == ["voicecall09"]
    assert controller.calls() == []
    assert events == []


def test_matching_modem_added_later_is_adopted() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([])
    assert timers.delays() == [1]

    transport.emit(MANAGER_IFACE, "ModemAdded", "/", *_modem())
    assert timers.delays() == [controller_mod.BRINGUP_TIMEOUT_SEC]
    assert transport.take("SetProperty").args == ("Powered", True)

    transport.emit(
        MANAGER_IFACE, "ModemAdded", "/",
        dbus.ObjectPath("/hfp/org/bluez/hci0/dev_11_22_33_44_55_66"),
        {"Type": "hfp", "Powered": True, "Online": True},
    )
    assert controller.state == CALLS_CONNECTING


def test_bring_up_watchdog_retries_a_silent_modem() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem(powered=True)])
    transport.take("SetProperty").on_reply()

    assert timers.delays() == [controller_mod.BRINGUP_TIMEOUT_SEC]
    timers.fire_all()
    assert timers.delays() == [1]


def test_stop_releases_every_watch_and_timer() -> None:
    controller, transport, timers, _changes, events = _ready()
    _ring(transport)

    controller.stop()

    assert all(match.removed for match in transport.matches)
    assert timers.entries == {}
    # Stopping the daemon is not a hang-up; no desktop "ended" popup churn.
    assert [event.kind for event in events] == ["call_incoming"]


def test_dbus_transport_never_activates_or_resolves_owners_synchronously() -> None:
    import dbus.lowlevel

    from blueferry.calls.ofono import (
        OFONO_CALL_TIMEOUT_SEC,
        DBusOfonoTransport,
        access_denied,
        service_missing,
    )

    class Bus:
        def __init__(self) -> None:
            self.sent = []
            self.receivers = []

        def send_message_with_reply(self, message, handler, timeout, **kwargs):
            self.sent.append((message, handler, timeout, kwargs))

        def call_async(self, *_args, **_kwargs):
            raise AssertionError("call_async cannot suppress bus activation")

        def add_signal_receiver(self, handler, **kwargs):
            self.receivers.append((handler, kwargs))
            return Match(handler, kwargs["dbus_interface"], kwargs["signal_name"], kwargs["path"])

        def get_object(self, *_args, **_kwargs):
            raise AssertionError("proxy creation resolves the owner synchronously")

    bus = Bus()
    transport = DBusOfonoTransport(lambda: bus)
    replies, errors = [], []
    transport.call(
        MODEM, MODEM_IFACE, "SetProperty", "sv", ("Powered", dbus.Boolean(True)),
        lambda *values: replies.append(values), errors.append,
    )
    transport.call("/", MANAGER_IFACE, "GetModems", "", (), replies.append, errors.append)
    owners = []
    transport.watch_owner(owners.append)
    bus.receivers[0][0]("org.ofono", ":1.5", "")

    message, handler, timeout, kwargs = bus.sent[0]
    assert message.get_auto_start() is False
    assert (message.get_destination(), message.get_path(), message.get_member()) == (
        "org.ofono", MODEM, "SetProperty",
    )
    assert message.get_args_list() == ["Powered", True]
    assert timeout == OFONO_CALL_TIMEOUT_SEC and kwargs == {"require_main_loop": True}

    # Reply and error mapping match dbus-python's call_async. The calls were
    # never sent, so they have no serial, and libdbus aborts the process
    # when a reply is built for serial 0 (Arch and Ubuntu enable those
    # checks). Stand-ins of the real message types carry the replies instead.
    class Reply(dbus.lowlevel.MethodReturnMessage):
        def __init__(self, *args) -> None:
            self.args = list(args)

        def get_args_list(self, **_kwargs):
            return self.args

    class Error(dbus.lowlevel.ErrorMessage):
        def __init__(self, name, *args) -> None:
            self.name = name
            self.args = list(args)

        def get_error_name(self):
            return self.name

        def get_args_list(self, **_kwargs):
            return self.args

    handler(Reply())
    bus.sent[1][1](Reply(dbus.Array([dbus.Struct((dbus.ObjectPath(MODEM), {"Type": "hfp"}))])))
    handler(Error("org.freedesktop.DBus.Error.AccessDenied", "no"))
    assert replies[0] == () and replies[1][0][0] == MODEM
    assert access_denied(errors[0]) and errors[0].get_dbus_message() == "no"

    assert bus.receivers[0][1]["arg0"] == "org.ofono"
    assert owners == [False]
    assert service_missing(_missing())
    assert not service_missing(RuntimeError("x"))


def test_access_denied_is_unavailable_and_logged_once(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="blueferry.calls.controller")
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    denied = _missing("org.freedesktop.DBus.Error.AccessDenied")
    for _ in range(3):
        transport.take("GetModems").on_error(denied)
        assert timers.delays() == [60]
        timers.fire_all()

    assert controller.state == CALLS_UNAVAILABLE
    messages = [record for record in caplog.records if "denies access" in record.getMessage()]
    assert len(messages) == 1 and messages[0].levelno == logging.INFO
    assert "at_console" in messages[0].getMessage()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_repeated_unexpected_discovery_error_warns_once(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="blueferry.calls.controller")
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    for _ in range(3):
        transport.take("GetModems").on_error(_missing("org.ofono.Error.Failed"))
        timers.fire_all()

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_owner_watch_is_released_on_stop_and_manager_rewatched_on_restart() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    owner = transport.owner_match
    first_manager = [m for m in transport.matches if m.interface == MANAGER_IFACE]

    transport.owner_handler(True)
    assert all(match.removed for match in first_manager)
    assert len([m for m in transport.matches
                if m.interface == MANAGER_IFACE and not m.removed]) == 2
    assert not owner.removed

    controller.stop()
    assert owner.removed
    assert all(match.removed for match in transport.matches)


def test_calls_beyond_the_tracking_limit_are_ignored() -> None:
    from blueferry.calls.model import MAX_TRACKED_CALLS

    controller, transport, _timers, _changes, events = _ready()
    for index in range(MAX_TRACKED_CALLS + 1):
        _ring(transport, state="active", path=f"{MODEM}/voicecall{index:02d}")

    assert len(controller.calls()) == MAX_TRACKED_CALLS
    assert len(events) == MAX_TRACKED_CALLS
    assert f"voicecall{MAX_TRACKED_CALLS:02d}" not in {c.call_id for c in controller.calls()}


def test_removal_of_a_foreign_modem_is_ignored() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    _ring(transport)

    transport.emit(
        MANAGER_IFACE, "ModemRemoved", "/",
        dbus.ObjectPath("/hfp/org/bluez/hci0/dev_11_22_33_44_55_66"),
    )

    assert controller.state == CALLS_READY
    assert len(controller.calls()) == 1


def test_bound_modem_missing_from_a_later_listing_ends_calls() -> None:
    controller, transport, timers, _changes, events = _ready()
    _ring(transport)
    controller._discover()

    transport.take("GetModems").on_reply([])

    assert controller.state == CALLS_SEARCHING
    assert controller.calls() == []
    assert events[-1].kind == "call_ended"
    assert timers.delays() == [1]


def test_bring_up_timeout_after_ready_is_harmless() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem(powered=True, online=True)])
    transport.emit(
        MODEM_IFACE, "PropertyChanged", MODEM, "Interfaces",
        dbus.Array([VOICE_CALL_MANAGER_IFACE], signature="s"),
    )
    assert controller.state == CALLS_READY

    # A watchdog armed before readiness and fired late must not retry.
    controller._bringup_timeout()
    assert controller.state == CALLS_READY
    assert timers.entries == {}


def test_synchronous_transport_failure_is_reported_like_an_async_one() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    failures = []

    def explode(*_args):
        raise dbus.exceptions.DBusException("gone", name="org.freedesktop.DBus.Error.Disconnected")

    transport.call = explode
    controller.hangup_all(_noop, failures.append)
    controller._discover()

    assert isinstance(failures[0], OperationFailedError)
    assert controller.state == CALLS_UNAVAILABLE


def test_stale_modem_signal_after_removal_is_ignored() -> None:
    controller, transport, _timers, _changes, _events = _ready()
    modem_watch = next(
        m for m in transport.matches if m.interface == MODEM_IFACE and not m.removed
    )
    transport.emit(MANAGER_IFACE, "ModemRemoved", "/", dbus.ObjectPath(MODEM))
    transport.pending.clear()

    modem_watch.handler("Powered", dbus.Boolean(False))

    assert controller.state == CALLS_SEARCHING
    assert transport.pending == []


def test_offline_modem_added_later_does_not_preempt_a_ready_one() -> None:
    other = "/hfp/org/bluez/hci0/x/dev_AA_BB_CC_DD_EE_FF"
    controller, transport, _timers, _changes, _events = _ready()
    # Same adapter, not voice ready: keep the working link.
    transport.emit(MANAGER_IFACE, "ModemAdded", "/", dbus.ObjectPath(other),
                   {"Type": "hfp", "Powered": False, "Online": False})

    assert controller.state == CALLS_READY
    assert transport.pending == []


def test_observed_progress_cancels_a_pending_backoff() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_error(
        dbus.exceptions.DBusException("x", name="org.ofono.Error.Failed")
    )
    assert timers.delays() == [1]

    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))

    assert 1 not in timers.delays()
    assert transport.take("SetProperty").args == ("Online", True)


def test_service_codes_never_reach_ofono() -> None:
    controller, transport, _timers, _changes, _events = _ready()

    with pytest.raises(InvalidArgumentsError, match="service codes"):
        controller.dial("**21*0791234567#", _noop, _noop)
    assert transport.pending == []


def test_repeated_bring_up_failures_log_at_info_only_once(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG, logger="blueferry.calls.controller")
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    rejected = dbus.exceptions.DBusException("x", name="org.ofono.Error.Failed")
    for _ in range(8):
        transport.take("GetModems").on_reply([_modem()])
        transport.take("SetProperty").on_error(rejected)
        timers.fire_all()

    def info(text):
        return [r for r in caplog.records if text in r.getMessage() and r.levelno == logging.INFO]

    def debug(text):
        return [r for r in caplog.records if text in r.getMessage() and r.levelno == logging.DEBUG]

    assert len(info("requesting iPhone HFP modem Powered")) == 1
    assert len(info("oFono rejected Powered")) == 1
    assert len(debug("requesting iPhone HFP modem Powered")) == 7
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    # Progress starts a new series, which is reported at INFO again.
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()
    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))
    assert len(info("requesting iPhone HFP modem Online")) == 1


def test_silent_modem_watchdog_repeats_quietly(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG, logger="blueferry.calls.controller")
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    for _ in range(4):
        transport.take("GetModems").on_reply([_modem(powered=True)])
        transport.take("SetProperty").on_reply()
        timers.fire_all()  # bring-up watchdog
        timers.fire_all()  # backoff -> rediscover

    silent = [r for r in caplog.records if "did not come online" in r.getMessage()]
    assert [r.levelno for r in silent] == [logging.INFO] + [logging.DEBUG] * 3


def test_acting_on_a_vanished_call_is_not_found() -> None:
    from blueferry.errors import NotFoundError

    controller, transport, _timers, _changes, _events = _ready()
    _ring(transport)
    failures = []

    controller.hangup("voicecall01", _noop, failures.append)
    transport.take("Hangup").on_error(
        _missing("org.freedesktop.DBus.Error.UnknownObject")
    )
    controller.hangup_all(_noop, failures.append)
    transport.take("HangupAll").on_error(
        _missing("org.freedesktop.DBus.Error.UnknownMethod")
    )

    assert isinstance(failures[0], NotFoundError)
    # Modem-level methods keep the generic call failure.
    assert isinstance(failures[1], OperationFailedError)


def test_stop_powers_down_the_modem_blueferry_powered() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()

    controller.stop()

    assert transport.sent == [(MODEM, MODEM_IFACE, "SetProperty", ("Powered", False))]
    assert isinstance(transport.sent[0][3][1], dbus.Boolean)
    controller.stop()
    assert len(transport.sent) == 1


def test_stop_powers_down_a_modem_found_already_powered() -> None:
    # For example after a crash: the previous process powered it.
    controller, transport, *_ = _ready()

    controller.set_enabled(False)

    assert transport.sent == [(MODEM, MODEM_IFACE, "SetProperty", ("Powered", False))]


def test_modem_that_powers_up_by_itself_is_powered_down_on_stop() -> None:
    controller, transport, _timers, _changes, _events = _build(reachable=lambda: False)
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))

    controller.stop()

    assert transport.sent == [(MODEM, MODEM_IFACE, "SetProperty", ("Powered", False))]


def test_disable_while_powering_up_powers_down_once_ofono_replies() -> None:
    controller, transport, timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    powering = transport.take("SetProperty")

    controller.set_enabled(False)
    assert transport.sent == []

    powering.on_reply()

    assert transport.sent == [(MODEM, MODEM_IFACE, "SetProperty", ("Powered", False))]
    assert controller.state == CALLS_DISABLED
    assert transport.pending == [] and timers.entries == {}


def test_failed_power_up_after_disable_sends_nothing() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    powering = transport.take("SetProperty")
    controller.set_enabled(False)

    powering.on_error(dbus.exceptions.DBusException("x", name="org.ofono.Error.Failed"))

    assert transport.sent == []


def test_stale_power_reply_while_calls_are_on_again_is_left_to_discovery() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    powering = transport.take("SetProperty")
    controller.set_enabled(False)
    controller.set_enabled(True)

    powering.on_reply()

    assert transport.sent == []


def test_stale_online_reply_after_disable_sends_nothing_more() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem(powered=True)])
    going_online = transport.take("SetProperty")
    controller.set_enabled(False)
    assert len(transport.sent) == 1

    going_online.on_reply()

    assert len(transport.sent) == 1


def test_stop_leaves_a_modem_never_seen_while_enabled_alone() -> None:
    controller, transport, *_ = _build(enabled=False)

    controller.stop()
    controller.set_enabled(False)

    assert transport.sent == [] and transport.pending == []


def test_ofono_restart_forgets_the_modem_it_powered() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()
    transport.owner_handler(True)

    controller.stop()

    assert transport.sent == []


def test_power_down_failure_does_not_break_stop() -> None:
    controller, transport, _timers, _changes, _events = _build()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()

    def broken(*_args):
        raise dbus.exceptions.DBusException("gone", name="org.freedesktop.DBus.Error.Disconnected")

    transport.send = broken
    controller.stop()

    assert all(match.removed for match in transport.matches)


def test_dbus_transport_send_is_fire_and_forget_and_flushed() -> None:
    from blueferry.calls.ofono import DBusOfonoTransport

    class Bus:
        def __init__(self) -> None:
            self.log = []

        def send_message(self, message):
            self.log.append(("send", message))

        def flush(self):
            self.log.append(("flush", None))

    bus = Bus()
    DBusOfonoTransport(lambda: bus).send(
        MODEM, MODEM_IFACE, "SetProperty", "sv", ("Powered", dbus.Boolean(False)),
    )

    assert [entry[0] for entry in bus.log] == ["send", "flush"]
    message = bus.log[0][1]
    assert message.get_auto_start() is False and message.get_no_reply() is True
    assert message.get_member() == "SetProperty"
    assert message.get_args_list() == ["Powered", False]


def _conflicted(conflict=lambda: True, reachable=lambda: True):
    transport = FakeTransport()
    timers = Timers()
    controller = CallController(
        enabled=True, mac=MAC, adapter="hci0", transport=transport,
        phone_reachable=reachable, hfp_conflict=conflict,
        schedule=timers.schedule, cancel=timers.cancel,
    )
    return controller, transport, timers


_REJECTED = dbus.exceptions.DBusException("x", name="org.ofono.Error.Failed")


def _fail_power(transport, timers, attempts: int) -> None:
    """Reject Powered ``attempts`` times, firing the backoff in between."""
    for attempt in range(attempts):
        if attempt:
            timers.fire_all()
        transport.take("GetModems").on_reply([_modem()])
        transport.take("SetProperty").on_error(_REJECTED)


def test_one_failed_power_request_is_not_a_bluez_conflict() -> None:
    controller, transport, timers = _conflicted()
    controller.start()

    _fail_power(transport, timers, 1)

    assert controller.state == CALLS_CONNECTING
    assert timers.delays() == [1]


def test_one_bring_up_timeout_is_not_a_bluez_conflict() -> None:
    controller, transport, timers = _conflicted()
    controller.start()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()

    timers.fire_all()  # bring-up watchdog: Powered never arrived

    assert controller.state == CALLS_CONNECTING
    assert timers.delays() == [1]


def test_repeated_power_failures_with_the_plugin_report_a_conflict(caplog) -> None:
    controller, transport, timers = _conflicted()
    controller.start()

    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)

    assert controller.state == "bluez_conflict"
    assert timers.delays() == [controller_mod.CONFLICT_RETRY_SEC]
    assert transport.pending == []
    with pytest.raises(CallsUnavailableError, match="HFP plugin"):
        controller.dial("0441234567", _noop, _noop)

    # The slow retry pages again without leaving the reported state, and
    # warns only once.
    timers.fire_all()
    transport.take("GetModems").on_reply([_modem()])
    assert controller.state == "bluez_conflict"
    transport.take("SetProperty").on_error(_REJECTED)
    assert controller.state == "bluez_conflict"
    assert timers.delays() == [controller_mod.CONFLICT_RETRY_SEC]
    assert len([r for r in caplog.records if "HFP hands-free plugin" in r.getMessage()]) == 1


def test_repeated_bring_up_timeouts_with_the_plugin_report_a_conflict() -> None:
    controller, transport, timers = _conflicted()
    controller.start()
    for _ in range(controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT):
        transport.take("GetModems").on_reply([_modem()])
        transport.take("SetProperty").on_reply()
        timers.fire_all()  # bring-up watchdog
        if controller.state != "bluez_conflict":
            timers.fire_all()  # backoff -> rediscover

    assert controller.state == "bluez_conflict"
    assert timers.delays() == [controller_mod.CONFLICT_RETRY_SEC]


def test_conflict_recovers_through_the_slow_retry_without_a_reconnect() -> None:
    controller, transport, timers = _conflicted()
    controller.start()
    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)

    timers.fire_all()
    transport.take("GetModems").on_reply([_modem()])
    transport.take("SetProperty").on_reply()
    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))

    assert controller.state == CALLS_CONNECTING
    assert transport.take("SetProperty").args == ("Online", True)


def test_conflict_ends_when_bluetoothd_drops_the_plugin() -> None:
    conflict = [True]
    controller, transport, timers = _conflicted(conflict=lambda: conflict[0])
    controller.start()
    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)
    assert controller.state == "bluez_conflict"

    conflict[0] = False
    timers.fire_all()
    _fail_power(transport, timers, 1)

    assert controller.state == CALLS_CONNECTING
    assert timers.delays() == [1]


def test_classic_returning_restarts_the_attempts_after_a_conflict() -> None:
    reachable = [True]
    controller, transport, timers = _conflicted(reachable=lambda: reachable[0])
    controller.poke()
    controller.start()
    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)
    assert controller.state == "bluez_conflict"

    reachable[0] = False
    controller.poke()
    reachable[0] = True
    controller.poke()

    assert transport.take("SetProperty").args == ("Powered", True)
    assert controller.state == CALLS_CONNECTING
    assert timers.delays() == [controller_mod.BRINGUP_TIMEOUT_SEC]


def test_progress_after_a_conflict_resumes_bring_up() -> None:
    controller, transport, timers = _conflicted()
    controller.start()
    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)
    assert controller.state == "bluez_conflict"

    transport.emit(MODEM_IFACE, "PropertyChanged", MODEM, "Powered", dbus.Boolean(True))

    assert transport.take("SetProperty").args == ("Online", True)
    assert controller.state == CALLS_CONNECTING


def test_without_a_conflict_power_failures_keep_backing_off() -> None:
    controller, transport, timers = _conflicted(conflict=lambda: False)
    controller.start()

    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT + 1)

    assert controller.state == CALLS_CONNECTING
    assert timers.delays() == [8]


def test_a_failing_conflict_check_is_treated_as_no_conflict() -> None:
    def broken() -> bool:
        raise OSError("proc gone")

    controller, transport, timers = _conflicted(conflict=broken)
    controller.start()
    _fail_power(transport, timers, controller_mod.POWER_ATTEMPTS_BEFORE_CONFLICT)

    assert controller.state == CALLS_CONNECTING
