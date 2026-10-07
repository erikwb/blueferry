"""GTK client work is dispatched without reaching the desktop bus."""
from __future__ import annotations

import threading

from blueferry.models import Thread
from blueferry.ui import client as client_module


class _SignalMatch:
    def remove(self) -> None:
        pass


class _Bus:
    def add_signal_receiver(self, *_args, **_kwargs):
        return _SignalMatch()


class _UnconfiguredSetup:
    def configuration(self):
        return type("Configuration", (), {"configured": False})()


def test_thread_snapshot_runs_off_the_ui_thread(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    monkeypatch.setattr(
        client_module.DaemonClient,
        "ensure_backend_current_async",
        lambda self: None,
    )

    idle_calls: list[int] = []

    def idle_add(callback, *args):
        idle_calls.append(threading.get_ident())
        callback(*args)
        return 1

    monkeypatch.setattr(client_module.GLib, "idle_add", idle_add)
    worker_threads: list[int] = []
    worker_started = threading.Event()
    release_worker = threading.Event()

    class Backend:
        def threads(self, limit):
            assert limit == 1000
            worker_threads.append(threading.get_ident())
            worker_started.set()
            assert release_worker.wait(3)
            return [Thread.from_dict({
                "key": "address:email:test@example.com",
                "name": "Test",
                "recipients": ["test@example.com"],
                "reply_ready": True,
            })]

    def backend_call(operation):
        return operation(Backend())

    current_thread = threading.get_ident()
    completed = threading.Event()
    received = []
    client = client_module.DaemonClient()
    monkeypatch.setattr(client, "_call_backend", backend_call)
    try:
        client.list_threads_async(
            lambda threads: (received.extend(threads), completed.set()),
            lambda _error: completed.set(),
        )
        assert worker_started.wait(3)
        release_worker.set()
        assert completed.wait(3)
    finally:
        client.stop()

    assert worker_threads and worker_threads[0] != current_thread
    assert idle_calls == worker_threads
    assert received[0].key == "address:email:test@example.com"


def test_contact_search_decodes_backend_destinations(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    calls = []

    class Backend:
        def find_contacts(self, query):
            calls.append(query)
            return [
                ("Alice", "15551234567"),
                ("Alice Work", "alice@example.com"),
            ]

    monkeypatch.setattr(
        client,
        "_call_backend",
        lambda operation: operation(Backend()),
    )
    monkeypatch.setattr(
        client,
        "_submit",
        lambda operation, on_ok, _on_err=None: on_ok(operation()),
    )
    received = []
    try:
        client.find_contacts_async(" Alice ", received.extend)
    finally:
        client.stop()

    assert calls == ["Alice"]
    assert received == [
        ("Alice", "15551234567"),
        ("Alice Work", "alice@example.com"),
    ]


def test_pending_send_does_not_block_thread_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    monkeypatch.setattr(
        client_module.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    send_started = threading.Event()
    release_send = threading.Event()
    snapshot_done = threading.Event()

    class Backend:
        def send(self, _recipient, _body):
            send_started.set()
            assert release_send.wait(3)
            return "/transfer/1"

        @staticmethod
        def threads(_limit):
            return []

    backend = Backend()
    client = client_module.DaemonClient()
    monkeypatch.setattr(
        client,
        "_call_backend",
        lambda operation: operation(backend),
    )
    try:
        client.send_message("+15551234567", "hello", lambda _value: None, None)
        assert send_started.wait(3)
        client.list_threads_async(lambda _threads: snapshot_done.set())
        assert snapshot_done.wait(1)
    finally:
        release_send.set()
        client.stop()


def test_selected_thread_deletion_uses_mutation_worker(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    observed = []

    class Backend:
        def delete_threads(self, keys):
            observed.append(list(keys))
            return len(keys)

    monkeypatch.setattr(
        client,
        "_call_backend",
        lambda operation: operation(Backend()),
    )
    submitted = {}

    def submit(operation, on_ok, _on_err=None, *, mutation=False):
        submitted["mutation"] = mutation
        on_ok(operation())

    monkeypatch.setattr(client, "_submit", submit)
    completed = []
    try:
        client.delete_threads_async(["one", "two"], completed.append, None)
    finally:
        client.stop()

    assert observed == [["one", "two"]]
    assert completed == [2]
    assert submitted == {"mutation": True}


def test_notification_actions_preference_uses_mutation_worker(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    observed = []

    class Backend:
        def set_ancs_notification_actions(self, enabled):
            observed.append(enabled)
            return enabled

    monkeypatch.setattr(
        client,
        "_call_backend",
        lambda operation: operation(Backend()),
    )
    submitted = {}

    def submit(operation, on_ok, _on_err=None, *, mutation=False):
        submitted["mutation"] = mutation
        on_ok(operation())

    monkeypatch.setattr(client, "_submit", submit)
    completed = []
    try:
        client.set_ancs_notification_actions_async(True, completed.append, None)
    finally:
        client.stop()

    assert observed == [True]
    assert completed == [True]
    assert submitted == {"mutation": True}


class _RecordingBus:
    def __init__(self) -> None:
        self.receivers: list[dict] = []

    def add_signal_receiver(self, handler, **keywords):
        self.receivers.append({"handler": handler, **keywords})
        return _SignalMatch()


def _tether_client(monkeypatch, backend):
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    monkeypatch.setattr(client, "_call_backend", lambda operation: operation(backend))
    submitted: list[bool] = []

    def submit(operation, on_ok, on_err=None, *, mutation=False):
        submitted.append(mutation)
        try:
            value = operation()
        except Exception as error:
            on_err(str(error))
            return
        on_ok(value)

    monkeypatch.setattr(client, "_submit", submit)
    return client, submitted


def test_tether_changed_signal_becomes_a_content_free_invalidation(monkeypatch) -> None:
    from blueferry.protocol import TETHER_IFACE

    bus = _RecordingBus()
    monkeypatch.setattr(client_module, "get_session_bus", lambda: bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    seen = []
    client.connect("tether-invalidated", lambda _client: seen.append(True))
    try:
        receiver = next(
            item for item in bus.receivers if item["signal_name"] == "TetherChanged"
        )
        assert receiver["dbus_interface"] == TETHER_IFACE
        receiver["handler"]()
    finally:
        client.stop()
    assert seen == [True]


def test_tether_calls_run_through_the_worker_and_map_missing_tether1(monkeypatch) -> None:
    from blueferry.client import TetherUnsupportedError
    from blueferry.tether_status import TetherStatus

    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    calls = []

    class Backend:
        def tether_state(self):
            raise TetherUnsupportedError("no Tether1")

        def tether_connect(self):
            calls.append("connect")
            return TetherStatus(state="connecting", enabled=True)

        def tether_disconnect(self):
            calls.append("disconnect")
            return TetherStatus(state="disconnecting", enabled=True)

        def tether_configure(self, enabled, autoconnect):
            calls.append(("configure", enabled, autoconnect))
            return TetherStatus(enabled=enabled, autoconnect=autoconnect)

    client, submitted = _tether_client(monkeypatch, Backend())
    received = []
    try:
        client.get_tether_async(received.append)
        client.set_tether_connected_async(True, received.append, None)
        client.set_tether_connected_async(False, received.append, None)
        client.configure_tether_async(True, False, received.append, None)
    finally:
        client.stop()

    assert received[0] is None  # an older daemon: hide the section
    assert [value.state for value in received[1:3]] == ["connecting", "disconnecting"]
    assert received[3].enabled is True
    assert calls == ["connect", "disconnect", ("configure", True, False)]
    # Reads use the read worker, changes the mutation worker.
    assert submitted == [False, True, True, True]


def test_tether_refusal_reaches_the_error_callback(monkeypatch) -> None:
    from blueferry.client import BackendError

    monkeypatch.setattr(client_module, "get_session_bus", _Bus)

    class Backend:
        def tether_connect(self):
            raise BackendError("Bluetooth tethering is turned off")

    client, _submitted = _tether_client(monkeypatch, Backend())
    errors = []
    try:
        client.set_tether_connected_async(True, lambda _value: None, errors.append)
    finally:
        client.stop()
    assert errors == ["Bluetooth tethering is turned off"]
