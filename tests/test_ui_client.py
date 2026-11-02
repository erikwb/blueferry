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


def test_shared_models_run_on_the_read_worker_and_finish_last(monkeypatch) -> None:
    class Backend:
        def notifications(self, limit):
            return {"enabled": True, "notifications": [], "limit": limit}

        def sync_call_history(self):
            return 3

    client, submitted = _recording_client(monkeypatch, Backend())
    events = []
    try:
        client.submit(lambda: client.notifications(5), lambda value: events.append(value),
                      events.append, lambda: events.append("finished"))
        client.submit(lambda: 1 / 0, events.append, lambda message: events.append(message),
                      lambda: events.append("finished"))
        client.submit(lambda: None)  # every callback is optional
        client.sync_call_history_async(events.append, events.append)
    finally:
        client.stop()
    assert events == [
        {"enabled": True, "notifications": [], "limit": 5}, "finished",
        "division by zero", "finished", 3,
    ]
    assert submitted == ["read", "read", "read", "mutation"]


def test_notifications_changed_is_forwarded_as_a_gobject_signal(monkeypatch) -> None:
    receivers = {}

    class Bus:
        def add_signal_receiver(self, handler, **kwargs):
            receivers[kwargs["signal_name"]] = handler
            return _SignalMatch()

    monkeypatch.setattr(client_module, "get_session_bus", Bus)
    monkeypatch.setattr(client_module, "SetupClient", _UnconfiguredSetup)
    client = client_module.DaemonClient()
    seen = []
    client.connect("notifications-changed", lambda _client: seen.append(True))
    try:
        receivers["NotificationsChanged"]()
    finally:
        client.stop()
    assert seen == [True]
    assert "CallsChanged" in receivers


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


def test_notification_rules_read_and_change_on_their_workers(monkeypatch) -> None:
    rule = {"bundle_id": "com.apple.mobilemail", "target": "https://mail.example.com/"}

    class Backend:
        def notification_open_map(self):
            return [rule]

        def set_notification_open_target(self, bundle_id, target):
            return [{"bundle_id": bundle_id, "target": target}]

        def remove_notification_open_target(self, bundle_id):
            return True

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.notification_open_map_async(results.append)
        client.set_notification_open_target_async(
            rule["bundle_id"], rule["target"], results.append, None)
        client.remove_notification_open_target_async(rule["bundle_id"], results.append, None)
    finally:
        client.stop()
    assert results == [[rule], [rule], True]
    assert submitted == ["read", "mutation", "mutation"]


def test_running_calls_are_read_and_controlled_on_their_workers(monkeypatch) -> None:
    from blueferry.models import CallsSnapshot

    actions = []

    class Backend:
        def calls(self):
            return CallsSnapshot(state="ready")

        def answer_call(self, call_id):
            actions.append(("answer", call_id))

        def hangup_call(self, call_id):
            actions.append(("hangup", call_id))

        def swap_calls(self):
            actions.append(("swap",))

        def send_call_tones(self, call_id, tones):
            actions.append(("tones", call_id, tones))

    client, submitted = _recording_client(monkeypatch, Backend())
    results = []
    try:
        client.calls_async(results.append)
        client.answer_call_async("voicecall01", results.append, None)
        client.swap_calls_async(results.append, None)
        client.hangup_call_async("voicecall01", results.append, None)
        client.send_call_tones_async("voicecall01", "12#", results.append, None)
    finally:
        client.stop()
    assert results == [CallsSnapshot(state="ready"), None, None, None, None]
    assert actions == [("answer", "voicecall01"), ("swap",), ("hangup", "voicecall01"),
                       ("tones", "voicecall01", "12#")]
    assert submitted == ["read", "mutation", "mutation", "mutation", "mutation"]
