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
