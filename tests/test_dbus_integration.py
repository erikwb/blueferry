"""Round-trip checks for the daemon's public session-D-Bus contract."""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import time
from dataclasses import replace

import dbus
import dbus.mainloop
import dbus.service
import pytest
from gi.repository import GLib

from blueferry import config
from blueferry.backend_operations import BackendDependencies
from blueferry.client import BackendClient
from blueferry.contacts import ContactsResolver
from blueferry.dbus_service import MessagesService
from blueferry.grouping import named_group_key
from blueferry.history import append_event
from blueferry.protocol import (
    BUS_NAME,
    EVENTS_IFACE,
    MESSAGES_API_VERSION,
    MESSAGES_IFACE,
    OBJECT_PATH,
)
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity
from blueferry.threads import group_confirmation_token

pytestmark = pytest.mark.private_dbus
_service_ids = itertools.count()


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error) -> None:
        pass


class _Policy:
    value = "messages"
    contacts_only = False

    def set(self, value: str) -> str:
        self.value = value
        return value

    def set_contacts_only(self, enabled: bool) -> bool:
        self.contacts_only = enabled
        return enabled


@pytest.fixture
def public_service():
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Testp{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)

    pending = queue.Queue()
    policy = _Policy()
    policy_changes = []

    def submit(operation, *, on_success, on_error) -> None:
        pending.put((operation, on_success, on_error))

    service = MessagesService(
        bus_name,
        _Sessions(),
        BackendDependencies(
            submit_obex=submit,
            status_provider=lambda: {"initializing": False},
            notification_policy=policy,
            on_notification_policy_changed=lambda: policy_changes.append(True),
        ),
    )
    try:
        yield name, pending, policy, policy_changes, service
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def _client(name: str):
    # These clients make synchronous calls from Python worker threads while
    # the test's main thread dispatches the service's GLib context. Keeping
    # their private connections off that context avoids concurrent libdbus
    # dispatch of the same connection.
    connection = dbus.SessionBus(
        private=True,
        mainloop=dbus.mainloop.NULL_MAIN_LOOP,
    )
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


def _request_in_thread(name, method, *args):
    outcome = {}
    def request():
        connection, interface = _client(name)
        try:
            outcome["value"] = getattr(interface, method)(*args, timeout=5)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()
    thread = threading.Thread(target=request)
    thread.start()
    return thread, outcome


def test_fresh_profile_unlock_and_snapshots_use_the_compatible_public_client(
    public_service, tmp_path, monkeypatch,
):
    name, _pending, _policy, _changes, service = public_service
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CONTACTS_DB", tmp_path / "contacts.sqlite")
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")

    class Wallet:
        def get_or_create(self, *, allow_prompt, cancellable=None):
            return b"K" * 32

    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )
    contacts = ContactsResolver(storage=storage)
    service.operations.dependencies = replace(
        service.operations.dependencies, storage=storage, contacts=contacts,
    )
    outcomes = []

    def first_launch():
        connection, interface = _client(name)
        client = BackendClient(interface_factory=lambda _: interface)
        try:
            assert client.status().to_dict()["api_version"] == MESSAGES_API_VERSION
            assert client.threads() == []
            assert client.unlock_storage()["storage_state"] == "ready"
            assert client.threads() == []
            append_event({
                "kind": "sms_received", "handle": "first", "sender_address": "+15551111111",
                "body": "first retained message",
            }, storage=storage)
            outcomes.append(client.threads()[0].messages[0].body)
        except Exception as error:
            outcomes.append(error)
        finally:
            connection.close()

    thread = threading.Thread(target=first_launch)
    thread.start()
    try:
        _dispatch_until(lambda: not thread.is_alive())
        assert outcomes == ["first retained message"]
    finally:
        storage.close()


def test_wallet_wait_keeps_status_available(public_service, tmp_path):
    name, _pending, _policy, _changes, service = public_service
    entered = threading.Event()
    release = threading.Event()
    class Wallet:
        def get_or_create(self, *, allow_prompt, cancellable=None):
            entered.set()
            assert release.wait(4), "wallet fixture was not released"
            return b"K" * 32
    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )
    service.operations.dependencies = replace(service.operations.dependencies, storage=storage)
    wallet_thread, wallet = _request_in_thread(name, "UnlockStorage")
    try:
        _dispatch_until(entered.is_set)
        status_thread, status = _request_in_thread(name, "GetStatus")
        _dispatch_until(lambda: not status_thread.is_alive())
        assert wallet_thread.is_alive()
        assert "error" not in status
        assert json.loads(str(status["value"]))["map"] is True
    finally:
        release.set()
        _dispatch_until(lambda: not wallet_thread.is_alive())
        storage.close()
    assert "error" not in wallet
    assert json.loads(str(wallet["value"]))["storage_state"] == "ready"


def test_projection_does_not_block_status_and_retries_after_history_changes(
    public_service, tmp_path, monkeypatch,
):
    name, _pending, _policy, _changes, service = public_service
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    first = {"kind": "sms_received", "handle": "1", "sender_address": "+15551111111", "body": "first"}
    append_event(first)
    entered = threading.Event()
    release = threading.Event()
    project = service.operations._project_conversations
    def blocked(events, contacts, stars):
        entered.set()
        assert release.wait(4), "projection fixture was not released"
        return project(events, contacts, stars)
    monkeypatch.setattr(service.operations, "_project_conversations", blocked)
    projection_thread, projection = _request_in_thread(name, "ListThreads", dbus.UInt32(10))
    try:
        _dispatch_until(entered.is_set)
        status_thread, status = _request_in_thread(name, "GetStatus")
        _dispatch_until(lambda: not status_thread.is_alive())
        assert projection_thread.is_alive()
        assert "error" not in status
        append_event({**first, "handle": "2", "body": "newer"})
    finally:
        release.set()
        _dispatch_until(lambda: not projection_thread.is_alive())
    assert "error" not in projection
    threads = json.loads(str(projection["value"]))
    assert [message["body"] for message in threads[0]["messages"]] == ["first", "newer"]


def test_checked_group_send_rejects_a_stale_snapshot_over_the_public_api(
    public_service, tmp_path, monkeypatch,
):
    name, pending, _policy, _changes, _service = public_service
    monkeypatch.setattr(config, "EVENTS_DB", tmp_path / "events.sqlite")
    key = named_group_key("Crew")
    initial = {
        "kind": "sms_sent", "group_key": key, "group_name": "Crew",
        "group_members": ["Alice", "Bob"],
        "group_recipients": ["+15551111111", "+15552222222"],
        "group_reply_ready": True, "handle": "1", "body": "fixture",
    }
    append_event(initial)
    snapshot_thread, snapshot = _request_in_thread(name, "ListThreads", dbus.UInt32(10))
    _dispatch_until(lambda: not snapshot_thread.is_alive())
    displayed = json.loads(str(snapshot["value"]))[0]
    approved = group_confirmation_token(displayed["recipients"], displayed["roster_warning_id"])
    updated = ["+15551111111", "+15553333333"]
    append_event({**initial, "kind": "group_route", "group_recipients": updated})
    stale_thread, stale = _request_in_thread(
        name, "SendToThreadChecked", key, "private draft", True, approved,
    )
    _dispatch_until(lambda: not stale_thread.is_alive())
    assert stale["error"].get_dbus_name().endswith(".ConfirmationRequired")
    assert pending.empty()
    legacy_thread, legacy = _request_in_thread(name, "SendToThread", key, "private draft", True)
    _dispatch_until(lambda: not legacy_thread.is_alive())
    assert legacy["error"].get_dbus_name().endswith(".ConfirmationRequired")
    assert pending.empty()
    send_thread, sent = _request_in_thread(
        name, "SendToThreadChecked", key, "private draft", True, group_confirmation_token(updated),
    )
    _dispatch_until(lambda: not pending.empty())
    # Never execute the Bluetooth operation; supply the fake worker result.
    _operation, success, _failure = pending.get_nowait()
    success("/transfer/test")
    _dispatch_until(lambda: not send_thread.is_alive())
    assert str(sent["value"]) == "/transfer/test"


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
            outcome["contacts_before"] = bool(
                interface.GetContactsOnlyNotifications(timeout=5)
            )
            outcome["contacts_after"] = bool(
                interface.SetContactsOnlyNotifications(True, timeout=5)
            )
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    client_thread = threading.Thread(target=change_policy)
    client_thread.start()
    _dispatch_until(lambda: not client_thread.is_alive())
    client_thread.join(timeout=1)

    assert outcome == {
        "before": "messages",
        "after": "none",
        "contacts_before": False,
        "contacts_after": True,
    }
    assert policy.value == "none"
    assert policy.contacts_only is True
    assert policy_changes == [True, True]


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
