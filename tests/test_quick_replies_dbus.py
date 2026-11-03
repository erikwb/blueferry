"""Calls1.DeclineWithMessage and SetCallQuickReplies over a private test bus."""
from __future__ import annotations

import itertools
import json
import os

import dbus
import dbus.service
import pytest

from blueferry.backend_operations import BackendDependencies
from blueferry.dbus_service import MessagesService
from blueferry.protocol import BUS_NAME, CALLS_IFACE, MESSAGES_IFACE
from tests.test_calls_dbus import _call, _raw_call
from tests.test_quick_replies import FakeCalls, FakeReplies, Obex, _Sessions

pytestmark = pytest.mark.private_dbus
_ids = itertools.count()


class Guard:
    """Records which rate buckets a call was charged to."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    def authorize(self, _sender, action) -> None:
        self.actions.append(action)

    def forget(self, _sender) -> None:
        pass

    def close(self) -> None:
        pass


def _service(guard=None):
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Replyp{os.getpid()}n{next(_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    calls, replies, obex = FakeCalls(), FakeReplies(), Obex()
    guard = guard(bus) if guard is not None else Guard()
    instance = MessagesService(
        bus_name, _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False},
            calls=calls, quick_replies=replies, submit_obex=obex,
        ),
        caller_guard=guard,
    )

    def close() -> None:
        instance.close()
        instance.remove_from_connection()
        bus.release_name(name)

    return (name, calls, replies, obex, guard), close


@pytest.fixture
def service():
    values, close = _service()
    try:
        yield values
    finally:
        close()


@pytest.fixture
def rate_limited_service():
    from blueferry.dbus_security import CallerGuard

    values, close = _service(lambda connection: CallerGuard(connection, clock=lambda: 1_000.0))
    try:
        yield values
    finally:
        close()


def test_decline_with_message_counts_as_hanging_up_and_a_send(service) -> None:
    name, calls, _replies, obex, guard = service

    outcome = _call(name, CALLS_IFACE, "DeclineWithMessage", "voicecall01", "Later")

    assert "error" not in outcome
    assert calls.requests == [("hangup", "voicecall01")] and len(obex.sent) == 1
    assert guard.actions[-2:] == ["calls-hangup", "send"]


def test_decline_with_message_errors_are_typed(service) -> None:
    name, calls, _replies, obex, _guard = service
    calls.number = ""
    unknown = _call(name, CALLS_IFACE, "DeclineWithMessage", "voicecall01", "Later")["error"]
    calls.number = "+41791234567"
    obex.error = RuntimeError("transfer failed")
    not_sent = _call(name, CALLS_IFACE, "DeclineWithMessage", "voicecall01", "Later")["error"]

    assert unknown.get_dbus_name() == "io.weirdware.BlueFerry.Error.InvalidArgs"
    assert not_sent.get_dbus_name() == "io.weirdware.BlueFerry.Error.DeclinedNotSent"
    assert "declined" in not_sent.get_dbus_message()
    assert calls.requests == [("hangup", "voicecall01")]  # only the second call declined


def test_quick_replies_round_trip_through_get_status(service) -> None:
    name, _calls, _replies, _obex, guard = service

    saved = json.loads(_call(name, CALLS_IFACE, "SetCallQuickReplies",
                             dbus.Array(["A", "B"], signature="s"))["value"])
    invalid = _call(name, CALLS_IFACE, "SetCallQuickReplies",
                    dbus.Array(["A", "A"], signature="s"))["error"]
    status = json.loads(_call(name, MESSAGES_IFACE, "GetStatus")["value"])

    assert saved == {"calls_quick_replies": ["A", "B"]}
    assert invalid.get_dbus_name() == "io.weirdware.BlueFerry.Error.InvalidArgs"
    assert status["calls_quick_replies"] == ["A", "B"]
    assert "settings" in guard.actions


def test_a_flood_of_tones_never_blocks_declining_with_a_message(rate_limited_service) -> None:
    name, calls, _replies, obex, _guard = rate_limited_service

    # Each request comes from a fresh connection, so only the daemon-wide
    # window applies: tones from any client exhaust "calls-control" for all.
    outcomes = [_call(name, CALLS_IFACE, "SendTones", "voicecall01", "1") for _ in range(61)]
    assert outcomes[60]["error"].get_dbus_name() == "io.weirdware.BlueFerry.Error.RateLimited"

    assert "error" not in _call(name, CALLS_IFACE, "DeclineWithMessage", "voicecall01", "Later")
    assert calls.requests[-1] == ("hangup", "voicecall01") and len(obex.sent) == 1


def test_decline_methods_reject_wrongly_typed_arguments(service) -> None:
    name, calls, replies, obex, guard = service
    before = list(replies.replies)

    for method, args, signature in (
        ("DeclineWithMessage", ("voicecall01",), "s"),
        ("DeclineWithMessage", ("voicecall01", 5), "si"),
        ("DeclineWithMessage", (dbus.ObjectPath("/voicecall01"), "Later"), "os"),
        # A plain string is not a one-entry list.
        ("SetCallQuickReplies", ("Later",), "s"),
        ("SetCallQuickReplies", ([1, 2],), "ai"),
    ):
        outcome = _raw_call(name, CALLS_IFACE, method, args, signature)
        assert outcome == {"error": "org.freedesktop.DBus.Error.InvalidArgs"}, (method, signature)

    assert calls.requests == [] and obex.sent == [] and replies.replies == before
    assert guard.actions == []  # refused before any rate bucket was charged
