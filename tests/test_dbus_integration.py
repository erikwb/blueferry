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
from blueferry.recipients import group_confirmation_token
from blueferry.settings_store import SettingsStore
from blueferry.storage_security import StorageSecurity, StorageUnavailableError

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


@pytest.mark.parametrize("handle,owner", [
    ("", ":1.1"), ("x" * 1025, ":1.1"), ("handle", ""),
    ("handle", "io.weirdware.BlueFerry.Gtk"), ("handle", ":invalid"),
])
def test_legacy_activation_rejects_invalid_requests(public_service, handle, owner):
    name, *_ = public_service
    thread, outcome = _request_in_thread(name, "OpenLegacyGtkMessage", handle, owner)
    _dispatch_until(lambda: bool(outcome))
    thread.join(1)
    assert outcome["error"].get_dbus_name().endswith(".InvalidArgs")


def test_legacy_activation_does_not_route_to_a_replaced_owner(public_service):
    from blueferry.client_activation import GTK_CLIENT

    name, *_ = public_service
    gtk_bus = dbus.SessionBus(private=True)
    gtk_name = dbus.service.BusName(GTK_CLIENT.desktop_id, bus=gtk_bus, do_not_queue=True)
    try:
        thread, outcome = _request_in_thread(name, "OpenLegacyGtkMessage", "handle", ":999.999")
        _dispatch_until(lambda: bool(outcome))
        thread.join(1)
        assert not outcome["value"]
    finally:
        del gtk_name  # release the name before closing the connection
        gtk_bus.close()


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


def test_passive_wallet_retry_keeps_status_available_and_announces_history(
    public_service, tmp_path,
):
    name, _pending, _policy, _changes, service = public_service
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Wallet:
        def get_or_create(self, *, allow_prompt, cancellable=None):
            calls.append(allow_prompt)
            entered.set()
            assert release.wait(4), "wallet fixture was not released"
            return b"K" * 32

    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )
    service.operations.dependencies = replace(
        service.operations.dependencies, storage=storage,
        on_storage_changed=service.emit_status,
        status_provider=lambda: {"initializing": False, "storage_state": storage.status.state},
    )
    connection = dbus.SessionBus(private=True)
    received = []
    match = connection.add_signal_receiver(
        lambda change: received.append(dict(change)), dbus_interface=EVENTS_IFACE,
        signal_name="HistoryChanged", bus_name=name, path=OBJECT_PATH,
    )
    try:
        service.retry_storage_unlock()
        _dispatch_until(entered.is_set)
        status_thread, status = _request_in_thread(name, "GetStatus")
        _dispatch_until(lambda: not status_thread.is_alive())
        assert storage.busy
        assert "error" not in status
        assert json.loads(str(status["value"]))["storage_state"] == "locked"
        assert not received
        service.retry_storage_unlock()
        assert calls == [False]
        release.set()
        _dispatch_until(lambda: bool(received))
        assert storage.status.can_write
        assert int(received[0]["revision"]) == 1
        service.retry_storage_unlock()
        assert calls == [False]
    finally:
        release.set()
        _dispatch_until(lambda: not storage.busy)
        match.remove()
        connection.close()
        storage.close()


@pytest.mark.parametrize("passive_times_out", [False, True])
def test_interactive_unlock_supersedes_passive_lookup(
    public_service, tmp_path, monkeypatch, passive_times_out,
):
    name, _pending, _policy, _changes, service = public_service
    entered = threading.Event()
    release = threading.Event()
    calls = []
    passive_cancellables = []
    timers = []
    monkeypatch.setattr(
        GLib, "timeout_add_seconds",
        lambda _seconds, callback: timers.append(callback) or len(timers),
    )
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)

    class Wallet:
        def get_or_create(self, *, allow_prompt, cancellable=None):
            calls.append(allow_prompt)
            if not allow_prompt:
                passive_cancellables.append(cancellable)
                entered.set()
                assert release.wait(4), "wallet fixture was not released"
                return b"P" * 32  # A cancelled result must never install this key.
            return b"K" * 32

    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )
    service.operations.dependencies = replace(service.operations.dependencies, storage=storage)
    wallet_thread = None
    try:
        service.retry_storage_unlock()
        _dispatch_until(entered.is_set)
        if passive_times_out:
            timers[0]()
            assert not storage.busy
            # Native cancellation has not returned yet. Polling must leave
            # capacity for the interactive request, not queue a second lookup.
            service.retry_storage_unlock()
            assert len(service._wallet_worker._pending) == 1
        wallet_thread, result = _request_in_thread(name, "UnlockStorage")
        _dispatch_until(lambda: len(service._wallet_worker._pending) == 2)
        assert passive_cancellables[0].is_cancelled()
        assert wallet_thread.is_alive()
        service.retry_storage_unlock()
        assert len(service._wallet_worker._pending) == 2

        other_thread, other = _request_in_thread(name, "UnlockStorage")
        _dispatch_until(lambda: not other_thread.is_alive())
        assert "already pending" in str(other["error"])
        assert calls == [False]

        release.set()
        _dispatch_until(lambda: not wallet_thread.is_alive())
        assert "error" not in result
        assert json.loads(str(result["value"]))["storage_state"] == "ready"
        assert calls == [False, True]
        verifier = StorageSecurity(
            settings=SettingsStore(tmp_path / "verifier.json"), key_provider=Wallet(), initialize=False,
        )
        verifier.refresh(allow_prompt=True)
        try:
            assert verifier.decrypt(
                storage.encrypt("interactive key", purpose="history-event-v1"),
                purpose="history-event-v1",
            ) == "interactive key"
        finally:
            verifier.close()
    finally:
        release.set()
        _dispatch_until(lambda: not service._wallet_worker.busy)
        if wallet_thread is not None:
            _dispatch_until(lambda: not wallet_thread.is_alive())
        storage.close()


@pytest.mark.parametrize("end_preparation", ["complete", "corruption", "shutdown"])
def test_preparation_keeps_dbus_responsive_and_holds_the_write_barrier(
    public_service, tmp_path, monkeypatch, end_preparation,
):
    name, _pending, _policy, _changes, service = public_service
    entered = threading.Event()
    release = threading.Event()
    committed = []
    calls = []
    timers = []
    main_thread = threading.get_ident()
    monkeypatch.setattr(
        GLib, "timeout_add_seconds",
        lambda _seconds, callback: timers.append(callback) or len(timers),
    )
    monkeypatch.setattr(GLib, "source_remove", lambda _source: True)

    class Wallet:
        def get_or_create(self, **_kwargs):
            calls.append(True)
            return b"K" * 32

    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )

    def prepare(candidate):
        assert threading.get_ident() != main_thread
        assert candidate.status.can_write
        assert not storage.status.can_write
        entered.set()
        assert release.wait(4), "preparation fixture was not released"
        return "prepared cache"

    def commit(data):
        assert threading.get_ident() == main_thread
        committed.append(data)

    service.operations.dependencies = replace(
        service.operations.dependencies, storage=storage,
        prepare_storage=prepare, on_storage_prepared=commit,
        status_provider=lambda: {"storage_state": storage.status.state},
    )
    unlock_thread = None
    try:
        service.retry_storage_unlock(initialize=True)
        _dispatch_until(entered.is_set)
        status_thread, status = _request_in_thread(name, "GetStatus")
        _dispatch_until(lambda: not status_thread.is_alive())
        assert "error" not in status
        assert json.loads(str(status["value"]))["storage_state"] == "locked"
        timers[0]()  # A wallet deadline must not release an active disk operation.
        assert storage.busy
        policy_thread, policy = _request_in_thread(name, "SetStoragePolicy", "plaintext")
        _dispatch_until(lambda: not policy_thread.is_alive())
        assert "already pending" in str(policy["error"])
        assert storage.status.policy == "encrypted"
        if end_preparation == "shutdown":
            service.close()
        else:
            unlock_thread, unlocked = _request_in_thread(name, "UnlockStorage")
            _dispatch_until(lambda: bool(storage._preparation_waiters))
            assert unlock_thread.is_alive()
            if end_preparation == "corruption":
                storage.fail_closed("independent authentication failure")
        release.set()
        _dispatch_until(lambda: not service._wallet_worker.busy)
        if unlock_thread is not None:
            _dispatch_until(lambda: not unlock_thread.is_alive())
            assert "error" not in unlocked
            expected = "ready" if end_preparation == "complete" else "error"
            assert json.loads(str(unlocked["value"]))["storage_state"] == expected
        assert calls == [True]
        assert committed == (["prepared cache"] if end_preparation == "complete" else [])
        assert storage.status.can_write == (end_preparation == "complete")
    finally:
        release.set()
        _dispatch_until(lambda: not service._wallet_worker.busy)
        storage.close()


def test_unlock_joining_locked_cleanup_still_gets_a_password_prompt(public_service, tmp_path):
    name, _pending, _policy, _changes, service = public_service
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Wallet:
        def get_or_create(self, *, allow_prompt, **_kwargs):
            calls.append(allow_prompt)
            if not allow_prompt:
                raise StorageUnavailableError("wallet locked")
            return b"K" * 32

    storage = StorageSecurity(
        settings=SettingsStore(tmp_path / "settings.json"), key_provider=Wallet(), initialize=False,
    )

    def prepare(candidate):
        if not candidate.status.can_read:
            entered.set()
            assert release.wait(4), "cleanup fixture was not released"

    service.operations.dependencies = replace(
        service.operations.dependencies, storage=storage, prepare_storage=prepare,
    )
    unlock_thread = None
    try:
        service.retry_storage_unlock(initialize=True)
        _dispatch_until(entered.is_set)
        unlock_thread, unlocked = _request_in_thread(name, "UnlockStorage")
        _dispatch_until(lambda: bool(storage._preparation_waiters))
        release.set()
        _dispatch_until(lambda: not unlock_thread.is_alive())
        assert "error" not in unlocked
        assert json.loads(str(unlocked["value"]))["storage_state"] == "ready"
        assert calls == [False, True]
    finally:
        release.set()
        _dispatch_until(lambda: not service._wallet_worker.busy)
        if unlock_thread is not None:
            _dispatch_until(lambda: not unlock_thread.is_alive())
        storage.close()


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
