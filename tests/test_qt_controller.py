"""Kirigami presentation state is built from typed clients without live I/O."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from blueferry.client import BackendError, TetherUnsupportedError
from blueferry.conversation_state import ConversationSnapshot
from blueferry.models import BackendStatus, Thread
from blueferry.qt.controller import BridgeController
from blueferry.setup_client import ConfigurationState
from blueferry.tether_status import TetherStatus


def _apply_threads(controller, threads):
    controller._apply_snapshot((
        ConversationSnapshot(threads=tuple(threads)), [thread.to_dict() for thread in threads],
    ))


class _Backend:
    def __init__(self):
        self.sent = []

    def status(self):
        return BackendStatus(daemon=True, map=True, contacts=4, storage_state="ready")

    def threads(self, limit=1000):
        return [
            Thread(
                key="address:email:test@example.com",
                name="Test",
                is_group=False,
                recipients=("test@example.com",),
                reply_ready=True,
                messages=(),
                last_ts="",
            )
        ]

    def find_contacts(self, query):
        assert query == "Ali"
        return [("Alice", "15551234567"), ("Alice Work", "alice@example.com")]

    def send(self, recipient, body):
        self.sent.append((recipient, body))
        return "/transfer/1"

    def set_group_participants(self, key, recipients):
        self.group_participants = (key, recipients)
        return object()

    def delete_threads(self, keys):
        self.deleted = list(keys)
        return len(keys)

    def mark_thread_read(self, key):
        self.marked = key
        return 1

    def set_thread_starred(self, key, starred):
        self.starred = (key, starred)
        return starred


def test_mark_thread_read_is_silent_and_not_busy():
    backend = _Backend()
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )

    controller.markThreadRead("address:email:test@example.com")
    controller._pool.waitForDone(1000)

    assert backend.marked == "address:email:test@example.com"
    assert controller.busy is False


def test_snapshot_converts_typed_client_models_for_qml():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )

    snapshot = controller._snapshot()
    controller._apply_snapshot(snapshot)

    assert controller.status["contacts"] == 4
    assert controller.threads[0]["key"] == "address:email:test@example.com"


def test_failed_thread_snapshot_preserves_last_successful_projection():
    class FailingBackend(_Backend):
        def status(self):
            return BackendStatus(
                daemon=True,
                map=True,
                contacts=4,
                storage_state="ready",
            )

        def threads(self, limit=1000):
            raise BackendError("thread snapshot unavailable")

    controller = BridgeController(
        backend=FailingBackend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    _apply_threads(controller, [Thread.from_dict({
        "key": "address:phone:15551234567", "name": "Kept",
    })])
    previous = controller.threads

    snapshot = controller._snapshot()
    controller._apply_snapshot(snapshot)

    assert controller.threads == previous
    assert controller.status["daemon"] is True
    assert controller.errorText == "thread snapshot unavailable"


def test_incompatible_initial_snapshot_displays_one_error_without_attempting_unlock(monkeypatch):
    from blueferry.protocol import backend_compatibility_error

    message = backend_compatibility_error({})

    class IncompatibleBackend(_Backend):
        def status(self):
            raise BackendError(message)

        def threads(self, limit=1000):
            raise BackendError(message)

    controller = BridgeController(
        backend=IncompatibleBackend(), setup=object(), subscribe=False, autostart=False,
    )
    unlocks = []
    monkeypatch.setattr(controller, "unlockStorage", lambda: unlocks.append(True))
    controller._apply_snapshot(controller._snapshot())
    assert controller.status["daemon"] is False
    assert controller.status["error"] == controller.errorText == message
    assert controller.status["storage_policy"] == ""
    assert controller.status["storage_state"] == "unavailable"
    assert controller.threads == []
    assert unlocks == []


def test_failed_status_preserves_the_latest_successful_storage_setting(monkeypatch):
    controller = BridgeController(
        backend=_Backend(), setup=object(), subscribe=False, autostart=False,
    )
    controller._apply_snapshot(controller._snapshot())
    monkeypatch.setattr(controller, "refresh", lambda: None)
    controller._storage_updated({"storage_policy": "none", "storage_state": "disabled"})
    controller._apply_snapshot((ConversationSnapshot(status_error="status timed out"), None))
    assert controller.status["storage_policy"] == "none"
    assert controller.status["storage_state"] == "unavailable"
    assert controller.errorText == "status timed out"


def test_failed_first_refresh_preserves_the_policy_loaded_at_startup(monkeypatch):
    controller = BridgeController(
        backend=_Backend(), setup=object(), subscribe=False, autostart=False,
    )
    status = BackendStatus(daemon=True, storage_policy="none", storage_state="disabled")
    monkeypatch.setattr(controller, "_run", lambda _operation, ready, _failed: ready((
        ConfigurationState(True, "02:00:00:00:00:01", "hci0", ""), status.to_dict(),
    )))
    monkeypatch.setattr(controller, "loadSetupState", lambda: None)
    monkeypatch.setattr(controller, "loadDevices", lambda _scan: None)
    monkeypatch.setattr(controller, "refresh", lambda: None)
    controller.start()
    controller._apply_snapshot((ConversationSnapshot(status_error="status timed out"), None))
    assert controller.status["storage_policy"] == "none"
    assert controller.status["storage_state"] == "unavailable"


def test_failed_capability_probe_is_loaded_and_pairable(monkeypatch):
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda _operation, _done, failed, **_kwargs: failed("probe failed"),
    )

    assert controller.compatibilityLoaded is False

    controller.loadSetupState()

    assert controller.compatibilityLoaded is True
    assert controller.compatibility["pairing_ready"] is True
    assert controller.compatibility["notifications_supported"] is False
    assert controller.compatibility["issue"] == "probe failed"


def test_onboarding_stage_signal_only_fires_when_stage_changes():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    changes = []
    controller.onboardingStageChanged.connect(lambda: changes.append(controller.onboardingStage))

    controller._update_onboarding_stage()
    controller._status = {"daemon": False}
    controller._update_onboarding_stage()
    assert changes == []

    controller._setup_loaded = True
    controller._update_onboarding_stage()
    assert len(changes) == 1

    controller._update_onboarding_stage()
    assert len(changes) == 1


def test_configured_mac_is_exposed_for_the_paired_phone_summary():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._configuration = ConfigurationState(
        configured=True,
        mac="02:00:00:00:00:01",
        adapter="hci0",
        path="",
    )

    assert controller.configuredMac == "02:00:00:00:00:01"


def test_notification_open_request_is_relayed_to_qml():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    opened = []
    controller.messageOpenRequested.connect(opened.append)

    controller._openMessageRequested("message-opaque-42")

    assert opened == ["message-opaque-42"]


def test_encrypted_storage_unlock_is_requested_only_once(monkeypatch):
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._status = {
        "daemon": True,
        "storage_policy": "encrypted",
        "storage_state": "locked",
    }
    calls = []
    monkeypatch.setattr(controller, "unlockStorage", lambda: calls.append(True))

    controller._maybe_unlock_storage()
    controller._maybe_unlock_storage()

    assert calls == [True]


def test_non_encrypted_storage_does_not_open_keyring(monkeypatch):
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._status = {
        "daemon": True,
        "storage_policy": "plaintext",
        "storage_state": "ready",
    }
    calls = []
    monkeypatch.setattr(controller, "unlockStorage", lambda: calls.append(True))

    controller._maybe_unlock_storage()

    assert calls == []


def test_new_message_searches_contacts_and_sends_directly(monkeypatch):
    backend = _Backend()
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    refreshes = []
    monkeypatch.setattr(controller, "refresh", lambda: refreshes.append(True))

    controller.findContacts(" Ali ")
    controller.sendMessage(" 15551234567 ", " hello ")

    assert controller.contactResults == [
        {"name": "Alice", "address": "15551234567"},
        {"name": "Alice Work", "address": "alice@example.com"},
    ]
    assert backend.sent == [("15551234567", "hello")]
    assert refreshes == [True]


def test_named_group_participants_are_forwarded_to_backend(monkeypatch):
    backend = _Backend()
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    monkeypatch.setattr(controller, "refresh", lambda: None)

    controller.setGroupParticipants(
        "group:named:test", [" +15551111111 ", "beau@example.com"]
    )

    assert backend.group_participants == (
        "group:named:test", ["+15551111111", "beau@example.com"]
    )


def test_selected_threads_are_forwarded_to_backend(monkeypatch):
    backend = _Backend()
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    refreshes = []
    monkeypatch.setattr(controller, "refresh", lambda: refreshes.append(True))

    controller.deleteThreads(["one", "two"])

    assert backend.deleted == ["one", "two"]
    assert refreshes == [True]


def test_pairing_uses_interactive_agent_and_accepts_matching_code(monkeypatch):
    observed = []

    class Setup:
        def complete_isolated(self, mac, *, confirmation, display, adapter=None, **_kwargs):
            observed.append((mac, adapter, confirmation(12345)))
            display(12345)
            return object()

        @staticmethod
        def complete(*_args, **_kwargs):
            raise AssertionError("Qt pairing must not host the D-Bus agent on its worker")

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    monkeypatch.setattr(controller, "loadDevices", lambda _scan: None)
    monkeypatch.setattr(controller, "loadSetupState", lambda: None)
    monkeypatch.setattr(controller, "refresh", lambda: None)
    passkeys = []

    def confirm(passkey):
        passkeys.append(passkey)
        controller.answerPairingConfirmation(True)

    controller.pairingConfirmationRequested.connect(confirm)

    controller._compatibility = {"adapter": "hci1"}
    controller.completePairing("02:00:00:00:00:01")

    assert passkeys == ["012345"]
    assert observed == [("02:00:00:00:00:01", "hci1", True)]


def test_pairing_rejects_when_confirmation_is_declined(monkeypatch):
    observed = []

    class Setup:
        def complete_isolated(self, _mac, *, confirmation, display, **_kwargs):
            observed.append(confirmation(None))
            return object()

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: operation(),
    )
    controller.pairingConfirmationRequested.connect(
        lambda passkey: controller.answerPairingConfirmation(False)
    )

    controller.completePairing("02:00:00:00:00:01")

    assert observed == [False]


def test_pairing_forwards_independent_pairing_modes(monkeypatch):
    from types import SimpleNamespace

    observed = []

    class Setup:
        def complete_isolated(self, _mac, **kwargs):
            observed.append((
                kwargs.get("compatibility_mode"),
                kwargs.get("explicit_pairing"),
            ))
            return SimpleNamespace(ancs_enabled=False)

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    monkeypatch.setattr(controller, "loadDevices", lambda _scan: None)
    monkeypatch.setattr(controller, "loadSetupState", lambda: None)
    monkeypatch.setattr(controller, "refresh", lambda: None)

    controller._compatibility = {
        "adapter": "hci0",
        "notifications_supported": True,
    }
    controller.completePairing("02:00:00:00:00:01", True, True)

    assert observed == [(True, True)]
    assert controller.compatibility["notifications_supported"] is True


def test_saved_pairing_policy_does_not_overwrite_adapter_capability(monkeypatch):
    from types import SimpleNamespace

    class Setup:
        @staticmethod
        def compatibility(_adapter=None):
            return SimpleNamespace(
                to_dict=lambda: {
                    "adapter": "hci0",
                    "hardware_supported": True,
                    "messages_supported": True,
                    "notifications_supported": True,
                    "pairing_ready": True,
                    "bearer_api_active": True,
                },
                bearer_api_active=True,
                notifications_active=True,
            )

        @staticmethod
        def configuration():
            return SimpleNamespace(
                configured=False,
                saved=True,
                bonded=False,
                mac="02:00:00:00:00:01",
                adapter="hci0",
                pairing_issue_report="",
                ancs_enabled=False,
            )

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )

    controller.loadSetupState()

    assert controller.targetSaved is True
    assert controller.configured is False
    assert controller.compatibility["notifications_supported"] is True


def test_compatibility_pairing_adjusts_only_the_qt_onboarding_view():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._compatibility = {
        "hardware_supported": True,
        "messages_supported": True,
        "notifications_supported": True,
        "bearer_api_active": True,
    }
    controller._configuration = ConfigurationState(
        configured=True,
        mac="02:00:00:00:00:01",
        adapter="hci0",
        path="",
        saved=True,
        ancs_enabled=False,
    )
    controller._setup_loaded = True
    controller._status = {
        "daemon": True,
        "map": True,
        "pbap": True,
        "verified_iphone_setup": ["message-notifications", "contacts"],
    }

    controller._update_onboarding_stage()

    assert controller.compatibility["notifications_supported"] is True
    assert controller.onboardingCompatibility["notifications_supported"] is False
    assert controller.onboardingStage == "ready-without-ancs"


def test_replacing_saved_target_is_forwarded_to_pairing_helper(monkeypatch):
    observed = []

    class Setup:
        def complete_isolated(
            self,
            mac,
            *,
            confirmation,
            display,
            adapter=None,
            replace_saved_mac="",
        ):
            observed.append((replace_saved_mac, mac, adapter))
            return object()

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    monkeypatch.setattr(controller, "loadDevices", lambda _scan: None)
    monkeypatch.setattr(controller, "loadSetupState", lambda: None)
    monkeypatch.setattr(controller, "refresh", lambda: None)

    controller._compatibility = {"adapter": "hci1"}
    controller.replaceAndPair(
        "02:00:00:00:00:01",
        "02:00:00:00:00:02",
    )

    assert observed == [
        ("02:00:00:00:00:01", "02:00:00:00:00:02", "hci1")
    ]
    assert controller.targetSaved is True


def test_pairing_issue_offer_stays_when_ancs_connects(monkeypatch, tmp_path) -> None:
    from blueferry import config, quirks_report

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    path = quirks_report.save_report(
        {"outcome": {"setup_complete": True, "ancs": True}},
        directory=tmp_path,
    )
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._status = {"ancs": True}

    controller._refresh_pairing_issue_report()

    assert controller.pairingIssueReport == str(path)


def test_select_adapter_reloads_compatibility_for_that_radio(monkeypatch) -> None:
    from types import SimpleNamespace

    calls = []

    class Setup:
        def compatibility(self, adapter=None):
            calls.append(adapter)
            return SimpleNamespace(
                to_dict=lambda: {
                    "adapter": adapter or "hci0",
                    "bearer_api_active": True,
                    "adapters": [
                        {"name": "hci0", "label": "hci0"},
                        {"name": "hci1", "label": "hci1"},
                    ],
                },
                bearer_api_active=True,
                notifications_active=True,
            )

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    controller._compatibility = {"adapter": "hci0"}
    controller._devices = [{"mac": "02:00:00:00:00:01", "display_name": "iPhone"}]
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    loaded = []
    monkeypatch.setattr(controller, "loadDevices", lambda scan: loaded.append(scan))

    controller.selectAdapter("hci1")

    assert calls == ["hci1"]
    assert controller.compatibility["adapter"] == "hci1"
    assert controller.devices == []
    assert loaded == [False]


def test_activating_bluetooth_reloads_the_selected_adapter_before_scanning(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    calls = []

    class Setup:
        def activate_bluez(self):
            calls.append("activate")
            return SimpleNamespace(active=True)

        def compatibility(self, adapter=None):
            calls.append(("compatibility", adapter))
            return SimpleNamespace(
                to_dict=lambda: {"adapter": adapter or "hci0", "bearer_api_active": True},
                bearer_api_active=True,
                notifications_active=True,
            )

        def configuration(self):
            return SimpleNamespace(
                configured=False,
                saved=False,
                mac="",
                adapter="hci0",
                pairing_issue_report="",
            )

    controller = BridgeController(
        backend=_Backend(),
        setup=Setup(),
        subscribe=False,
        autostart=False,
    )
    controller._compatibility = {"adapter": "hci1"}
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    loaded = []
    monkeypatch.setattr(controller, "loadDevices", lambda scan: loaded.append(scan))

    controller.activateBluetooth()

    assert calls == ["activate", ("compatibility", "hci1")]
    assert controller.compatibility["adapter"] == "hci1"
    assert loaded == [True]


def test_group_send_requires_explicit_approval_and_rejects_changed_roster(monkeypatch):
    controller = BridgeController(backend=_Backend(), setup=object(), subscribe=False, autostart=False)
    _apply_threads(controller, [Thread.from_dict({
        "key": "group:test", "name": "Group", "is_group": True, "reply_ready": True,
        "recipients": ["+15551111111", "+15552222222"],
    })])
    queued = []
    prompts = []
    monkeypatch.setattr(controller, "_run", lambda *args, **kwargs: queued.append(args))
    controller.groupConfirmationRequested.connect(lambda *args: prompts.append(args))
    controller.sendThread("group:test", "draft", False)
    assert not queued
    assert prompts == [("group:test", "draft", "+15551111111\n+15552222222")]
    thread = controller._state.threads[0]
    _apply_threads(controller, [
        replace(thread, recipients=(*thread.recipients, "+15553333333")),
    ])
    controller.sendThread("group:test", "draft", True)
    assert not queued
    assert "group changed" in controller.errorText
    controller.sendThread("group:test", "draft", False)
    controller.sendThread("group:test", "draft", True)
    assert len(queued) == 1


def test_draft_completion_is_emitted_only_after_success(monkeypatch):
    backend = _Backend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    _apply_threads(controller, backend.threads())
    queued = []
    completed = []
    monkeypatch.setattr(controller, "_run", lambda *args, **kwargs: queued.append(args))
    monkeypatch.setattr(controller, "refresh", lambda: None)
    controller.threadSendSucceeded.connect(lambda *args: completed.append(args))
    controller.messageSendSucceeded.connect(lambda *args: completed.append(args))
    key = controller.threads[0]["key"]
    controller.sendThread(key, " draft ", False)
    controller.sendMessage(" new recipient ", " new draft ")
    assert completed == []
    queued[0][1]("sent")
    assert completed == [(key, " draft ")]
    queued[1][1]("sent")
    assert completed[-1] == (" new recipient ", " new draft ")


def test_contact_search_rejects_repeated_query_stale_results_and_errors(monkeypatch):
    controller = BridgeController(backend=_Backend(), setup=object(), subscribe=False, autostart=False)
    pending = []
    monkeypatch.setattr(controller, "_run", lambda *args, **_kw: pending.append(args))
    controller.findContacts("Ali")
    controller.findContacts("Bob")
    controller.findContacts("Ali")
    pending[0][1]([("Old Alice", "old@example.com")])
    pending[1][2]("stale failure")
    assert controller.contactResults == []
    assert controller.errorText == ""
    pending[2][1]([("Alice", "alice@example.com")])
    assert controller.contactResults == [{"name": "Alice", "address": "alice@example.com"}]
    controller.findContacts("")
    pending[2][1]([("Late Alice", "late@example.com")])
    assert controller.contactResults == []


def test_successful_group_reply_reuses_confirmation_until_roster_changes(monkeypatch):
    controller = BridgeController(backend=_Backend(), setup=object(), subscribe=False, autostart=False)
    _apply_threads(controller, [Thread.from_dict({
        "key": "group:test", "is_group": True, "reply_ready": True,
        "recipients": ["+15551111111", "+15552222222"],
    })])
    pending = []
    prompts = []
    monkeypatch.setattr(controller, "_run", lambda *args, **_kw: pending.append(args))
    monkeypatch.setattr(controller, "refresh", lambda: None)
    controller.groupConfirmationRequested.connect(lambda *args: prompts.append(args))
    controller.sendThread("group:test", "first", False)
    controller.sendThread("group:test", "first", True)
    pending.pop()[1]("sent")
    controller.sendThread("group:test", "second", False)
    assert len(prompts) == 1
    assert len(pending) == 1
    thread = controller._state.threads[0]
    _apply_threads(controller, [
        replace(thread, recipients=(*thread.recipients, "+15553333333")),
    ])
    controller.sendThread("group:test", "third", False)
    assert len(prompts) == 2
    assert len(pending) == 1


def test_proximity_lock_setting_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _Backend()
    calls = []

    def set_proximity_lock(enabled, grace):
        calls.append((enabled, grace))
        return {"proximity_lock": "idle", "proximity_lock_enabled": enabled,
                "proximity_lock_grace_sec": grace}

    backend.set_proximity_lock = set_proximity_lock
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    changes = []
    controller.statusChanged.connect(lambda: changes.append(True))

    controller.setProximityLock(True, 120)

    assert calls == [(True, 120)]
    assert controller.status["proximity_lock_enabled"] is True
    assert controller.status["proximity_lock_grace_sec"] == 120
    assert changes == [True]


def _call_history_controller(backend, *, enabled=True):
    controller = BridgeController(
        backend=backend, setup=object(), subscribe=False, autostart=False,
    )
    controller._status = {"call_history_enabled": enabled}
    return controller


def test_call_history_is_inert_until_the_backend_reports_the_opt_in():
    class Backend(_Backend):
        def call_history(self, _limit):
            raise AssertionError("disabled feature must not be queried")

    controller = _call_history_controller(Backend(), enabled=False)

    controller.watchCallHistory(True)
    controller.loadCallHistory()
    controller.syncCallHistory()
    controller._callHistoryInvalidated()
    controller._pool.waitForDone(1000)

    assert controller.callHistoryEnabled is False
    assert controller.callHistory == []


def _inline_runs(controller, monkeypatch):
    """Run controller tasks inline; record that they went through _run."""
    runs = []

    def run(operation, done=None, failed=None, **kwargs):
        runs.append(kwargs.get("busy", True))
        try:
            value = operation()
        except Exception as error:
            (failed or controller._operation_failed)(str(error))
        else:
            if done is not None:
                done(value)

    monkeypatch.setattr(controller, "_run", run)
    return runs


def test_call_history_uses_worker_tasks_without_touching_conversation_errors(monkeypatch):
    from blueferry.models import CallHistoryEntry

    class Backend(_Backend):
        synced = 0

        def call_history(self, limit):
            assert limit == 200
            return [CallHistoryEntry.from_dict({
                "direction": "missed", "timestamp": "2026-09-28T09:00:00+00:00",
                "address": "+15551230002", "name": None, "contact_name": None,
            })]

        def sync_call_history(self):
            type(self).synced += 1
            return 1

    controller = _call_history_controller(Backend())
    runs = _inline_runs(controller, monkeypatch)
    changes = []
    controller.callHistoryChanged.connect(lambda: changes.append(True))

    controller.loadCallHistory()
    assert runs == [], "nothing is fetched until the page is shown"
    controller._call_history_watched = True
    controller.syncCallHistory()

    assert runs == [True, False], "sync shows busy; the list refresh does not"
    assert Backend.synced == 1
    assert controller.callHistory[0]["caller"] == "+15551230002"
    assert controller.callHistory[0]["missed"] is True
    assert controller.callHistoryError == ""
    assert controller.errorText == ""
    assert changes == [True]


def test_call_history_failure_is_reported_on_its_own_property(monkeypatch):
    class Backend(_Backend):
        def call_history(self, _limit):
            raise BackendError("storage is locked")

    controller = _call_history_controller(Backend())
    _inline_runs(controller, monkeypatch)

    controller.watchCallHistory(True)

    assert "storage is locked" in controller.callHistoryError
    assert controller.errorText == ""


def test_content_free_invalidation_reloads_only_a_shown_list(monkeypatch):
    controller = _call_history_controller(_Backend())
    started = []
    monkeypatch.setattr(controller._call_history_timer, "start", lambda: started.append(1))
    monkeypatch.setattr(controller, "loadCallHistory", lambda: None)

    controller._callHistoryInvalidated()
    controller.watchCallHistory(True)
    controller._callHistoryInvalidated()

    assert started == [1]


def test_opening_loads_and_closing_forgets_call_records(monkeypatch):
    from blueferry.models import CallHistoryEntry

    class Backend(_Backend):
        loads = 0

        def call_history(self, _limit):
            type(self).loads += 1
            return [CallHistoryEntry.from_dict({
                "direction": "incoming", "timestamp": "2026-09-28T09:00:00+00:00",
                "address": "+15551230002",
            })]

    controller = _call_history_controller(Backend())
    runs = _inline_runs(controller, monkeypatch)
    assert Backend.loads == 0

    controller.watchCallHistory(True)
    assert Backend.loads == 1 and len(controller.callHistory) == 1

    controller.watchCallHistory(False)
    assert controller.callHistory == []
    controller._callHistoryInvalidated()
    assert Backend.loads == 1 and runs == [False]


def test_late_reply_after_close_is_discarded(monkeypatch):
    controller = _call_history_controller(_Backend())
    captured = {}

    def run(operation, done=None, failed=None, **_kwargs):
        captured["done"] = done

    monkeypatch.setattr(controller, "_run", run)
    controller.watchCallHistory(True)
    controller.watchCallHistory(False)

    captured["done"]([{"caller": "late"}])

    assert controller.callHistory == []


def test_call_history_opt_in_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _Backend()
    calls = []

    def set_call_history(enabled, popups):
        calls.append((enabled, popups))
        return {"call_history_enabled": enabled, "missed_call_notifications": popups}

    backend.set_call_history = set_call_history
    controller = BridgeController(
        backend=backend, setup=object(), subscribe=False, autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    changes = []
    controller.statusChanged.connect(lambda: changes.append(True))

    controller.setCallHistory(True, False)

    assert calls == [(True, False)]
    assert controller.callHistoryEnabled is True
    assert controller.status["missed_call_notifications"] is False
    assert changes == [True]


def _synchronous(controller, monkeypatch):
    def run(operation, on_done=None, on_failed=None, **_kwargs):
        try:
            value = operation()
        except Exception as error:
            (on_failed or controller._operation_failed)(str(error))
            return
        if on_done is not None:
            on_done(value)

    monkeypatch.setattr(controller, "_run", run)


def test_notification_click_rules_are_loaded_edited_and_exposed_to_qml(monkeypatch):
    from blueferry.notification_open_map import open_map_entries

    class _RuleBackend(_Backend):
        def __init__(self):
            super().__init__()
            self.rules = {"com.slack": "slack.desktop"}
            self.calls = []

        def notification_open_map(self):
            self.calls.append(("list",))
            return open_map_entries(self.rules)

        def set_notification_open_target(self, bundle_id, target):
            self.calls.append(("set", bundle_id, target))
            self.rules[bundle_id] = target
            return open_map_entries(self.rules)

        def remove_notification_open_target(self, bundle_id):
            self.calls.append(("remove", bundle_id))
            return self.rules.pop(bundle_id, None) is not None

    backend = _RuleBackend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    _synchronous(controller, monkeypatch)
    changes = []
    controller.notificationOpenMapChanged.connect(lambda: changes.append(True))

    controller.loadNotificationOpenMap()
    assert controller.notificationOpenMap == [
        {"bundle_id": "com.slack", "target": "slack.desktop", "kind": "desktop"},
    ]

    controller.setNotificationOpenTarget(" net.whatsapp.WhatsApp ", " https://web.whatsapp.com ")
    controller.setNotificationOpenTarget("", "https://ignored.example")
    controller.removeNotificationOpenTarget("com.slack")

    assert backend.calls == [
        ("list",),
        ("set", "net.whatsapp.WhatsApp", "https://web.whatsapp.com"),
        ("remove", "com.slack"),
        ("list",),
    ]
    assert controller.notificationOpenMap == [
        {"bundle_id": "net.whatsapp.WhatsApp", "target": "https://web.whatsapp.com", "kind": "url"},
    ]
    assert len(changes) == 3


def test_rejected_notification_click_rule_is_reported_without_changing_the_list(monkeypatch):
    class _RejectingBackend(_Backend):
        def set_notification_open_target(self, _bundle_id, _target):
            raise BackendError("target must be an http(s) URL or a desktop entry ID")

    controller = BridgeController(
        backend=_RejectingBackend(), setup=object(), subscribe=False, autostart=False,
    )
    _synchronous(controller, monkeypatch)

    controller.setNotificationOpenTarget("com.example.App", "javascript:alert(1)")

    assert controller.notificationOpenMap == []
    assert "http(s) URL" in controller.errorText


class _MediaBackend(_Backend):
    def __init__(self):
        super().__init__()
        self.media_commands = []
        self.now_playing_calls = 0

    def now_playing(self):
        self.now_playing_calls += 1
        return {"enabled": True, "available": True, "track": {"title": "Song"}}

    def send_media_command(self, command):
        self.media_commands.append(command)


def test_now_playing_is_fetched_only_when_media_is_enabled(monkeypatch):
    backend = _MediaBackend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)

    def run_inline(operation, on_done=None, on_failed=None, *, busy=True):
        assert busy is False
        try:
            value = operation()
        except Exception as error:
            on_failed(str(error))
        else:
            on_done(value)

    monkeypatch.setattr(controller, "_run", run_inline)
    changes = []
    controller.nowPlayingChanged.connect(lambda: changes.append(True))

    controller.refreshNowPlaying()
    assert backend.now_playing_calls == 0
    assert controller.nowPlaying == {}

    controller._status = {"media_control_enabled": True}
    controller.refreshNowPlaying()
    assert backend.now_playing_calls == 1
    assert controller.nowPlaying["track"]["title"] == "Song"
    assert changes == [True]

    # A failed read clears stale track details instead of showing them.
    backend.now_playing = lambda: (_ for _ in ()).throw(BackendError("gone"))
    controller.refreshNowPlaying()
    assert controller.nowPlaying == {}


def test_media_command_runs_off_the_ui_thread_without_busy_state():
    backend = _MediaBackend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    controller.sendMediaCommand("next")
    controller.sendMediaCommand("  ")
    assert controller.busy is False
    controller._pool.waitForDone(1000)
    assert backend.media_commands == ["next"]


def test_media_control_setting_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _MediaBackend()
    calls = []

    def set_media_control(enabled):
        calls.append(enabled)
        return {"media_control_enabled": enabled, "media_control_available": False}

    backend.set_media_control = set_media_control
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    changes = []
    controller.statusChanged.connect(lambda: changes.append(True))

    controller.setMediaControl(True)

    assert calls == [True]
    assert controller.status["media_control_enabled"] is True
    assert changes == [True]
    # The bar follows at once instead of waiting for the next signal.
    assert backend.now_playing_calls == 1


def test_ancs_actions_setting_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _Backend()
    calls = []
    backend.set_ancs_notification_actions = lambda enabled: calls.append(enabled) or enabled
    controller = BridgeController(
        backend=backend,
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )

    controller.setAncsNotificationActions(True)

    assert calls == [True]
    assert controller.status["ancs_actions_preference"] is True
    assert controller.status["ancs_actions"] is True


def test_failed_ancs_actions_change_reports_the_saved_value_again(monkeypatch):
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )
    controller._status["ancs_actions_preference"] = True
    monkeypatch.setattr(
        controller,
        "_run",
        lambda _operation, _on_done=None, on_failed=None, **_kwargs: on_failed("refused"),
    )
    changes = []
    controller.statusChanged.connect(lambda: changes.append(True))

    controller.setAncsNotificationActions(False)

    assert controller.status["ancs_actions_preference"] is True
    assert controller.errorText == "refused"
    assert changes == [True]


def test_checking_bluetooth_le_again_reprobes_the_selected_adapter(
    monkeypatch,
) -> None:
    """The LE stage offers only a re-check: BlueFerry cannot switch LE on."""
    from types import SimpleNamespace

    calls = []

    class Setup:
        def compatibility(self, adapter=None):
            calls.append(("compatibility", adapter))
            return SimpleNamespace(
                to_dict=lambda: {"adapter": adapter, "le_disabled": False},
                bearer_api_active=True,
                notifications_active=True,
            )

        def configuration(self):
            return SimpleNamespace(
                configured=False,
                saved=False,
                mac="",
                adapter="hci0",
                pairing_issue_report="",
            )

    controller = BridgeController(
        backend=_Backend(), setup=Setup(), subscribe=False, autostart=False,
    )
    controller._compatibility = {"adapter": "hci1", "le_disabled": True}
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )

    controller.loadSetupState()

    assert calls == [("compatibility", "hci1")]
    assert controller.compatibility["le_disabled"] is False
    assert not hasattr(controller, "enableLowEnergy")


def test_optional_calls_are_exposed_without_touching_a_disabled_backend():
    from blueferry.models import CallsSnapshot

    class CallsBackend:
        def __init__(self):
            self.requests = []

        def calls(self):
            self.requests.append(("calls",))
            return CallsSnapshot.from_dict({"state": "ready", "calls": [
                {"call_id": "voicecall01", "state": "incoming", "contact_name": "Alice"},
            ]})

        def dial(self, number):
            self.requests.append(("dial", number))
            return "voicecall02"

        def answer_call(self, call_id):
            self.requests.append(("answer", call_id))

    backend = CallsBackend()
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)

    controller.refreshCalls()
    controller._pool.waitForDone(1000)
    assert backend.requests == []
    assert controller.callsState == "disabled"

    controller._status = {"calls_enabled": True, "calls_state": "ready"}
    controller._apply_calls(backend.calls())
    assert controller.phoneCalls[0]["display_peer"] == "Alice"
    assert controller.phoneCalls[0]["ringing"] is True
    assert controller.callsState == "ready"

    controller.dialCall("  0441234567 ")
    controller.answerCall("voicecall01")
    controller.dialCall("   ")
    controller._pool.waitForDone(1000)
    assert ("dial", "0441234567") in backend.requests
    assert ("answer", "voicecall01") in backend.requests
    assert ("dial", "") not in backend.requests


def test_failed_or_disabled_calls_refresh_follows_the_status_state():
    from blueferry.models import CallsSnapshot

    controller = BridgeController(backend=object(), setup=object(), subscribe=False, autostart=False)
    changes = []
    controller.phoneCallsChanged.connect(lambda: changes.append(controller.callsState))
    controller._status = {"calls_enabled": True, "calls_state": "ready"}
    controller._apply_calls(CallsSnapshot.from_dict({"state": "ready", "calls": [
        {"call_id": "voicecall01", "state": "active"},
    ]}))

    # ListCalls failed although status said ready: no stale call, no "ready".
    controller._calls_unavailable("boom")
    assert controller.phoneCalls == [] and controller.callsState == "unavailable"

    controller._status = {"calls_enabled": False, "calls_state": "disabled"}
    controller.refreshCalls()
    assert controller.callsState == "disabled"
    assert changes == ["ready", "unavailable", "disabled"]
    assert not hasattr(controller, "sendCallTones")


def test_calls_opt_in_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _Backend()
    toggles = []

    def set_calls_enabled(enabled):
        toggles.append(enabled)
        return {"calls_enabled": enabled, "calls_state": "unavailable"}

    backend.set_calls_enabled = set_calls_enabled
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    refreshed = []
    monkeypatch.setattr(controller, "refreshCalls", lambda: refreshed.append(True))

    controller.setCallsEnabled(True)

    assert toggles == [True]
    assert controller.status["calls_enabled"] is True
    assert refreshed == [True]


def test_status_refreshes_the_call_list_only_when_the_call_state_changes(monkeypatch):
    controller = BridgeController(
        backend=_Backend(), setup=object(), subscribe=False, autostart=False,
    )
    refreshed = []
    monkeypatch.setattr(controller, "refreshCalls", lambda: refreshed.append(True))

    def apply(**fields):
        controller._apply_snapshot((
            ConversationSnapshot(status=BackendStatus(daemon=True, storage_state="ready", **fields)),
            None,
        ))

    apply(calls_enabled=True, calls_state="connecting")
    apply(calls_enabled=True, calls_state="connecting", map=True)
    assert refreshed == [True]
    apply(calls_enabled=True, calls_state="ready")
    assert refreshed == [True, True]
    apply(calls_enabled=False)
    assert len(refreshed) == 2  # nothing listed, nothing to clear


def test_status_from_a_daemon_without_the_calls_setting_omits_it():
    # The settings page offers the checkbox only when the key is present.
    controller = BridgeController(
        backend=_Backend(), setup=object(), subscribe=False, autostart=False,
    )

    def apply(reported):
        controller._apply_snapshot((
            ConversationSnapshot(status=BackendStatus.from_dict(
                {"daemon": True, "storage_state": "ready", **reported}
            )),
            None,
        ))

    apply({})
    assert "calls_enabled" not in controller.status

    apply({"calls_enabled": False})
    assert controller.status["calls_enabled"] is False


def test_battery_warning_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _Backend()
    backend.set_phone_battery_warning = lambda enabled: enabled
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    monkeypatch.setattr(
        controller, "_run",
        lambda operation, on_done=None, *_args, **_kwargs: on_done(operation()),
    )

    controller.setPhoneBatteryWarning(True)

    assert controller.status["phone_battery_warning"] is True


def test_mpris_setting_is_forwarded_and_merged_into_status(monkeypatch):
    backend = _MediaBackend()
    backend.set_mpris_player = lambda enabled: {"media_mpris_enabled": enabled}
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)
    monkeypatch.setattr(
        controller,
        "_run",
        lambda operation, on_done=None, *_args, **_kwargs: (
            on_done(operation()) if on_done is not None else operation()
        ),
    )
    controller.setMprisPlayer(True)
    assert controller.status["media_mpris_enabled"] is True

class _TetherBackend(_Backend):
    def __init__(self, *, fail: str | None = None, unsupported: bool = False):
        super().__init__()
        self.fail = fail
        self.unsupported = unsupported
        self.tether_calls: list[str] = []

    def _tether(self, name, state):
        self.tether_calls.append(name)
        if self.unsupported:
            raise TetherUnsupportedError("the running backend does not support tethering")
        if self.fail:
            raise BackendError(self.fail)
        return TetherStatus.from_dict(state)

    def tether_state(self):
        return self._tether("state", {"state": "off"})

    def tether_connect(self):
        return self._tether("connect", {"state": "connecting", "backend": "networkmanager"})

    def tether_disconnect(self):
        return self._tether("disconnect", {"state": "disconnecting"})


def _tether_controller(backend):
    return BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)


def _settle(controller) -> None:
    """Finish worker tasks and deliver their queued results to the controller."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication

    # A GUI application: later QML tests in the same process need one, and
    # Qt cannot replace a plain QCoreApplication once it exists.
    application = QCoreApplication.instance() or QGuiApplication([])
    controller._pool.waitForDone(1000)
    for _ in range(5):
        application.processEvents()


def test_tether_state_is_exposed_with_shared_guidance():
    backend = _TetherBackend()
    controller = _tether_controller(backend)
    assert controller.tether == {"available": False}

    controller.refreshTether()
    _settle(controller)

    assert controller.tether["available"] is True
    assert controller.tether["state"] == "off"
    assert "Not sharing" in controller.tether["summary"]
    assert backend.tether_calls == ["state"]
    assert controller.busy is False


def test_tether_toggle_sends_exactly_one_explicit_request():
    backend = _TetherBackend()
    controller = _tether_controller(backend)

    controller.setTetherEnabled(True)
    controller.setTetherEnabled(True)  # ignored while the first is pending
    _settle(controller)

    assert backend.tether_calls == ["connect"]
    assert controller.tether["state"] == "connecting"
    assert controller.tether["pending"] is False

    controller.setTetherEnabled(False)
    _settle(controller)
    assert backend.tether_calls == ["connect", "disconnect"]


def test_backend_without_tether1_hides_the_control():
    controller = _tether_controller(_TetherBackend(unsupported=True))
    controller._tether = {"available": True, "state": "off"}
    controller.refreshTether()
    _settle(controller)
    assert controller.tether == {"available": False}
    assert controller.errorText == ""


def test_transient_tether_read_failure_keeps_the_last_state():
    backend = _TetherBackend()
    controller = _tether_controller(backend)
    controller.refreshTether()
    _settle(controller)
    before = dict(controller.tether)

    backend.fail = "status timed out"
    controller.refreshTether()
    _settle(controller)

    assert controller.tether == before
    assert controller.tether["available"] is True
    assert "status timed out" in controller.errorText


def test_failed_tether_request_reports_and_clears_pending():
    backend = _TetherBackend(fail="the iPhone is not connected over Bluetooth yet")
    controller = _tether_controller(backend)
    controller._tether = {"available": True, "state": "off"}

    controller.setTetherEnabled(True)
    _settle(controller)

    assert controller.tether["pending"] is False
    assert "not connected over Bluetooth" in controller.errorText
