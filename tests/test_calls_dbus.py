"""Calls1 over the isolated private bus, with an inert call controller.

Runs only inside ``dbus-run-session --config-file=tests/dbus-test.conf``. The
controller is a fake: nothing reaches oFono, BlueZ, or a phone.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time

import dbus
import dbus.mainloop
import dbus.service
import pytest
from gi.repository import GLib

from blueferry.backend_operations import BackendDependencies
from blueferry.client import BackendClient
from blueferry.dbus_service import MessagesService
from blueferry.errors import InvalidArgumentsError, OperationFailedError
from blueferry.protocol import (
    BUS_NAME,
    CALLS_IFACE,
    EVENTS_IFACE,
    MESSAGES_API_VERSION,
    MESSAGES_IFACE,
    OBJECT_PATH,
)

pytestmark = pytest.mark.private_dbus
_service_ids = itertools.count()


class _Sessions:
    map = None
    pbap = None
    map_path = ""

    @staticmethod
    def report_error(_error) -> None:
        pass


class FakeCalls:
    enabled = True

    def __init__(self) -> None:
        self.requests: list[tuple] = []

    def snapshot(self):
        return {"calls_enabled": True, "calls_state": "ready", "calls_available": True}

    def list_calls(self):
        return {"state": "ready", "calls": [{
            "call_id": "voicecall01", "state": "incoming", "direction": "incoming",
            "number": "+41791234567", "network_name": "", "contact_name": "Alice",
            "multiparty": False, "emergency": False, "first_seen": "2026-01-01T00:00:00+00:00",
        }]}

    def dial(self, number, success, failure):
        if number == "fail":
            failure(OperationFailedError("Call", RuntimeError("secret +41 detail")))
            return
        if not number.isdigit():
            raise InvalidArgumentsError("phone number is invalid")
        self.requests.append(("dial", number))
        success("voicecall07")

    def answer(self, call_id, success, _failure):
        self.requests.append(("answer", call_id))
        success(None)

    def hangup(self, call_id, success, _failure):
        self.requests.append(("hangup", call_id))
        success(None)

    def hangup_all(self, success, _failure):
        self.requests.append(("hangup_all",))
        success(None)

    def send_tones(self, call_id, tones, success, _failure):
        self.requests.append(("tones", call_id, tones))
        success(None)

    def swap(self, success, _failure):
        self.requests.append(("swap",))
        success(None)

    def hold_and_answer(self, success, _failure):
        self.requests.append(("hold_and_answer",))
        success(None)


def _service(calls, caller_guard=None):
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Callsp{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name,
        _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False},
            calls=calls,
        ),
        caller_guard=caller_guard(bus) if caller_guard is not None else None,
    )
    return bus, name, service


@pytest.fixture
def calls_service():
    calls = FakeCalls()
    bus, name, service = _service(calls)
    try:
        yield name, calls, service
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


@pytest.fixture
def disabled_service():
    bus, name, service = _service(None)
    try:
        yield name, service
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


@pytest.fixture
def rate_limited_service():
    from blueferry.dbus_security import CallerGuard

    now = [1_000.0]
    calls = FakeCalls()
    bus, name, service = _service(
        calls, caller_guard=lambda connection: CallerGuard(connection, clock=lambda: now[0]),
    )
    try:
        yield name, calls, now
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


def _call(name, interface_name, method, *args):
    """Call from a worker thread while the test thread dispatches the service."""
    outcome = {}

    def request():
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            interface = dbus.Interface(connection.get_object(name, OBJECT_PATH), interface_name)
            outcome["value"] = getattr(interface, method)(*args, timeout=5)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=request)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join()
    return outcome


def test_disabled_calls_fail_with_a_typed_error(disabled_service) -> None:
    name, _service = disabled_service

    for method, args in (("ListCalls", ()), ("Dial", ("0441234567",)), ("HangupAll", ())):
        outcome = _call(name, CALLS_IFACE, method, *args)
        assert outcome["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.CallsDisabled"

    status = json.loads(_call(name, MESSAGES_IFACE, "GetStatus")["value"])
    # A backend without the calls dependency reports no calls keys at all.
    assert not any(key.startswith("calls_") for key in status)
    assert status["api_version"] == MESSAGES_API_VERSION


def test_calls_round_trip_through_the_public_interface(calls_service) -> None:
    name, calls, _service = calls_service

    listed = json.loads(_call(name, CALLS_IFACE, "ListCalls")["value"])
    assert listed["calls"][0]["contact_name"] == "Alice"
    assert str(_call(name, CALLS_IFACE, "Dial", "0441234567")["value"]) == "voicecall07"
    assert "error" not in _call(name, CALLS_IFACE, "Answer", "voicecall01")
    assert "error" not in _call(name, CALLS_IFACE, "SendTones", "voicecall01", "1#")
    assert "error" not in _call(name, CALLS_IFACE, "Hangup", "voicecall01")
    assert "error" not in _call(name, CALLS_IFACE, "SwapCalls")
    assert "error" not in _call(name, CALLS_IFACE, "HoldAndAnswer")
    assert "error" not in _call(name, CALLS_IFACE, "HangupAll")

    assert calls.requests == [
        ("dial", "0441234567"), ("answer", "voicecall01"), ("tones", "voicecall01", "1#"),
        ("hangup", "voicecall01"), ("swap",), ("hold_and_answer",), ("hangup_all",),
    ]
    status = json.loads(_call(name, MESSAGES_IFACE, "GetStatus")["value"])
    assert status["calls_available"] is True


def test_call_failures_are_typed_and_redacted(calls_service) -> None:
    name, _calls, _service = calls_service

    invalid = _call(name, CALLS_IFACE, "Dial", "0800;ATH")["error"]
    failed = _call(name, CALLS_IFACE, "Dial", "fail")["error"]

    assert invalid.get_dbus_name() == "io.weirdware.BlueFerry.Error.InvalidArgs"
    assert failed.get_dbus_name() == "io.weirdware.BlueFerry.Error.CallFailed"
    assert "+41" not in failed.get_dbus_message()


def test_calls_changed_signal_carries_no_content(calls_service) -> None:
    name, _calls, service = calls_service
    connection = dbus.SessionBus(private=True)
    received = []
    match = connection.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=EVENTS_IFACE,
        signal_name="CallsChanged",
        bus_name=name,
        path=OBJECT_PATH,
    )
    try:
        service.emit_calls_changed()
        _dispatch_until(lambda: bool(received))
    finally:
        match.remove()
        connection.close()

    assert received == [()]


def test_shared_client_checks_compatibility_through_messages1(calls_service) -> None:
    name, _calls, _service = calls_service
    outcome = {}

    def request():
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = BackendClient(
                interface_factory=lambda interface: dbus.Interface(
                    connection.get_object(name, OBJECT_PATH), interface,
                ),
            )
            outcome["snapshot"] = client.calls()
            outcome["dial"] = client.dial("0441234567")
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=request)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join()

    assert "error" not in outcome, outcome.get("error")
    assert outcome["snapshot"].available
    assert outcome["snapshot"].calls[0].display_peer == "Alice"
    assert outcome["dial"] == "voicecall07"


def test_dial_quota_is_enforced_over_the_bus_without_blocking_answer(rate_limited_service) -> None:
    name, calls, now = rate_limited_service

    outcomes = [_call(name, CALLS_IFACE, "Dial", "0441234567") for _ in range(7)]

    assert all("error" not in outcome for outcome in outcomes[:6])
    assert outcomes[6]["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.RateLimited"
    assert "error" not in _call(name, CALLS_IFACE, "Answer", "voicecall01")
    assert calls.requests.count(("dial", "0441234567")) == 6
    now[0] += 61
    assert "error" not in _call(name, CALLS_IFACE, "Dial", "0441234567")


def test_set_calls_enabled_saves_through_the_daemon_hook() -> None:
    toggles = []

    def configure(enabled):
        toggles.append(enabled)
        return {"calls_enabled": enabled, "calls_state": "unavailable" if enabled else "disabled"}

    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Callsp{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False},
            set_calls_enabled=configure,
        ),
    )
    try:
        enabled = json.loads(_call(name, CALLS_IFACE, "SetCallsEnabled", True)["value"])
        disabled = json.loads(_call(name, CALLS_IFACE, "SetCallsEnabled", False)["value"])
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert toggles == [True, False]
    assert enabled["calls_enabled"] is True and enabled["calls_state"] == "unavailable"
    assert disabled == {"calls_enabled": False, "calls_state": "disabled"}


def test_set_calls_enabled_without_a_daemon_hook_is_not_ready(disabled_service) -> None:
    name, _service = disabled_service

    outcome = _call(name, CALLS_IFACE, "SetCallsEnabled", True)

    assert outcome["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.NotReady"


class PhoneStatusCalls(FakeCalls):
    def snapshot(self):
        return {
            **super().snapshot(),
            "phone_battery_level": 40,
            "phone_signal_strength": 80,
            "phone_network_name": "Sunrise",
            "phone_network_status": "registered",
        }


def test_phone_status_is_unicast_in_get_status_and_signalled_without_content() -> None:
    bus, name, service = _service(PhoneStatusCalls())
    connection = dbus.SessionBus(private=True)
    received = []
    match = connection.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=EVENTS_IFACE,
        signal_name="StatusChanged",
        bus_name=name,
        path=OBJECT_PATH,
    )
    try:
        status = json.loads(_call(name, MESSAGES_IFACE, "GetStatus")["value"])
        # The daemon announces a phone-status change with the existing,
        # argument-free StatusChanged; values are fetched with GetStatus.
        service.emit_status()
        _dispatch_until(lambda: bool(received))
    finally:
        match.remove()
        connection.close()
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert status["phone_battery_level"] == 40
    assert status["phone_signal_strength"] == 80
    assert status["phone_network_name"] == "Sunrise"
    assert status["phone_network_status"] == "registered"
    assert status["api_version"] == MESSAGES_API_VERSION
    assert received == [()]
    assert MessagesService.StatusChanged._dbus_signature == ""


def test_disabled_calls_report_unknown_phone_status(disabled_service) -> None:
    name, _service = disabled_service

    status = json.loads(_call(name, MESSAGES_IFACE, "GetStatus")["value"])

    for key in (
        "phone_battery_level", "phone_signal_strength",
        "phone_network_name", "phone_network_status",
    ):
        assert key in status and status[key] is None
