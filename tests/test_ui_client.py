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


def test_contact_photos_are_saved_on_the_mutation_worker(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()

    class Backend:
        def set_contact_photos(self, enabled):
            return {"contact_photos": enabled}

    monkeypatch.setattr(client, "_call_backend", lambda operation: operation(Backend()))
    submitted = {}

    def submit(operation, on_ok, _on_err=None, *, mutation=False):
        submitted["mutation"] = mutation
        on_ok(operation())

    monkeypatch.setattr(client, "_submit", submit)
    completed = []
    try:
        client.set_contact_photos_async(True, completed.append, None)
    finally:
        client.stop()

    assert completed == [{"contact_photos": True}]
    assert submitted == {"mutation": True}


def test_otp_autocopy_is_saved_on_the_mutation_worker(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()

    class Backend:
        def set_otp_autocopy(self, enabled):
            return {"otp_autocopy": enabled}

    monkeypatch.setattr(client, "_call_backend", lambda operation: operation(Backend()))
    submitted = {}

    def submit(operation, on_ok, _on_err=None, *, mutation=False):
        submitted["mutation"] = mutation
        on_ok(operation())

    monkeypatch.setattr(client, "_submit", submit)
    completed = []
    try:
        client.set_otp_autocopy_async(True, completed.append, None)
    finally:
        client.stop()

    assert completed == [{"otp_autocopy": True}]
    assert submitted == {"mutation": True}


def _recording_client(monkeypatch, backend):
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    monkeypatch.setattr(client, "_call_backend", lambda operation: operation(backend))
    submitted = []

    def submit(operation, on_ok, on_err=None, *, mutation=False, local=False):
        submitted.append("local" if local else "mutation" if mutation else "read")
        try:
            value = operation()
        except Exception as error:
            on_err(str(error))
        else:
            on_ok(value)

    monkeypatch.setattr(client, "_submit", submit)
    return client, submitted


def test_feature_switches_read_and_save_on_their_workers(monkeypatch) -> None:
    saved = []

    class Backend:
        def features(self):
            return {"mark_read_on_dismiss": {"value": True}}

        def set_feature(self, name, enabled):
            saved.append((name, enabled))
            return "restart-required"

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.features_async(results.append)
        client.set_feature_async("mark_read_on_dismiss", False, results.append, None)
    finally:
        client.stop()
    assert results == [{"mark_read_on_dismiss": {"value": True}}, "restart-required"]
    assert saved == [("mark_read_on_dismiss", False)]
    assert submitted == ["read", "mutation"]


def test_restart_follows_mutations_and_the_doctor_runs_locally(monkeypatch) -> None:
    from blueferry.doctor_report import DoctorReport

    restarted = []
    monkeypatch.setattr(client_module, "restart_backend", lambda: restarted.append(True))
    client, submitted = _recording_client(monkeypatch, object())
    results, errors = [], []
    report = DoctorReport(True, False, "OK")

    def broken():
        raise RuntimeError("doctor crashed")

    try:
        client.restart_backend_async(results.append, errors.append)
        client.run_doctor_async(results.append, errors.append, runner=lambda: report)
        client.run_doctor_async(results.append, errors.append, runner=broken)
    finally:
        client.stop()
    assert restarted == [True]
    assert results == [None, report] and errors == ["doctor crashed"]
    assert submitted == ["mutation", "local", "local"]


def test_doctor_work_does_not_wait_for_daemon_reads(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "get_session_bus", _Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    delivered = threading.Event()

    def idle_add(callback, *args):
        callback(*args)
        delivered.set()
        return 1

    monkeypatch.setattr(client_module.GLib, "idle_add", idle_add)
    release_read = threading.Event()
    client = client_module.DaemonClient()
    monkeypatch.setattr(client, "_call_backend", lambda _operation: release_read.wait(5))
    reports = []
    read_done = threading.Event()
    try:
        client.get_status_async(lambda _status: read_done.set())  # holds the read worker
        client.run_doctor_async(reports.append, None, runner=lambda: "report")
        assert delivered.wait(3), "the doctor waited behind a daemon read"
        assert reports == ["report"]
    finally:
        release_read.set()
        assert read_done.wait(3)
        client.stop()


def test_notification_sync_preference_uses_mutation_worker(monkeypatch) -> None:
    class Backend:
        def set_mirror_notification_removals(self, enabled):
            return enabled

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.set_mirror_notification_removals_async(True, results.append, None)
    finally:
        client.stop()
    assert results == [True] and submitted == ["mutation"]


def test_battery_warning_preference_uses_mutation_worker(monkeypatch) -> None:
    class Backend:
        def set_phone_battery_warning(self, enabled):
            return enabled

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.set_phone_battery_warning_async(True, results.append, None)
    finally:
        client.stop()
    assert results == [True] and submitted == ["mutation"]


def test_manual_reconnect_uses_mutation_worker(monkeypatch) -> None:
    class Backend:
        def reconnect_phone(self):
            return "started"

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.reconnect_phone_async(results.append, None)
    finally:
        client.stop()
    assert results == ["started"] and submitted == ["mutation"]
