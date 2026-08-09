"""Round-trip checks for the daemon's public session-D-Bus contract."""
from __future__ import annotations

import json
import os
import queue
import threading
import time

import dbus
import dbus.service
import pytest
from gi.repository import GLib

from blueferry.dbus_service import MessagesService
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, MESSAGES_IFACE, OBJECT_PATH

pytestmark = pytest.mark.private_dbus


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error) -> None:
        pass


class _Policy:
    value = "messages"

    def set(self, value: str) -> str:
        self.value = value
        return value


@pytest.fixture
def public_service():
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Testp{os.getpid()}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)

    pending = queue.Queue()
    policy = _Policy()
    policy_changes = []

    def submit(operation, *, on_success, on_error) -> None:
        pending.put((operation, on_success, on_error))

    service = MessagesService(
        bus_name,
        _Sessions(),
        submit_obex=submit,
        status_provider=lambda: {"initializing": False},
        notification_policy=policy,
        on_notification_policy_changed=lambda: policy_changes.append(True),
    )
    try:
        yield name, pending, policy, policy_changes, service
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def _client(name: str):
    connection = dbus.SessionBus(private=True)
    interface = dbus.Interface(
        connection.get_object(name, OBJECT_PATH), MESSAGES_IFACE
    )
    return connection, interface


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    """Run the service's GLib context until a client-observable event occurs."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


def test_pending_send_does_not_block_status_or_its_eventual_reply(
    public_service,
) -> None:
    name, pending, _policy, _policy_changes, _service = public_service
    outcome = {}

    def send() -> None:
        connection, interface = _client(name)
        try:
            outcome["transfer"] = str(interface.Send(
                "+15551234567", "hello", timeout=5
            ))
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    send_thread = threading.Thread(target=send)
    send_thread.start()
    _dispatch_until(lambda: not pending.empty())
    # The queued closure would call send_message. Never execute it: this test
    # supplies the worker completion itself and exercises only the D-Bus API.
    _inert_operation, send_succeeded, _send_failed = pending.get_nowait()

    status_outcome = {}

    def get_status() -> None:
        connection, interface = _client(name)
        try:
            status_outcome["status"] = json.loads(str(
                interface.GetStatus(timeout=5)
            ))
        except Exception as error:
            status_outcome["error"] = error
        finally:
            connection.close()

    status_thread = threading.Thread(target=get_status)
    status_thread.start()
    _dispatch_until(lambda: not status_thread.is_alive())
    status_thread.join(timeout=1)

    assert send_thread.is_alive()
    assert "error" not in status_outcome
    status = status_outcome["status"]
    assert status["map"] is True
    assert status["pbap"] is True
    send_succeeded("/transfer/test")
    _dispatch_until(lambda: not send_thread.is_alive())
    send_thread.join(timeout=1)

    assert not send_thread.is_alive()
    assert outcome == {"transfer": "/transfer/test"}


def test_notification_policy_round_trips_without_profile_io(public_service) -> None:
    name, _pending, policy, policy_changes, _service = public_service
    outcome = {}

    def change_policy() -> None:
        connection, interface = _client(name)
        try:
            outcome["before"] = str(interface.GetNotificationPolicy(timeout=5))
            outcome["after"] = str(
                interface.SetNotificationPolicy("none", timeout=5)
            )
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    client_thread = threading.Thread(target=change_policy)
    client_thread.start()
    _dispatch_until(lambda: not client_thread.is_alive())
    client_thread.join(timeout=1)

    assert outcome == {"before": "messages", "after": "none"}
    assert policy.value == "none"
    assert policy_changes == [True]


def test_live_signal_contains_only_an_opaque_revision(public_service) -> None:
    name, _pending, _policy, _policy_changes, service = public_service
    connection = dbus.SessionBus(private=True)
    received = []
    match = connection.add_signal_receiver(
        lambda change: received.append(dict(change)),
        dbus_interface=EVENTS_IFACE,
        signal_name="HistoryChanged",
        bus_name=name,
        path=OBJECT_PATH,
    )
    try:
        service.emit_history_changed()
        _dispatch_until(lambda: bool(received))
    finally:
        match.remove()
        connection.close()

    assert set(received[0]) == {"revision"}
    assert int(received[0]["revision"]) == 1
