"""Kirigami presentation state is built from typed clients without live I/O."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from blueferry.models import BackendStatus, Thread
from blueferry.qt.controller import BridgeController


class _Backend:
    def __init__(self):
        self.sent = []

    def status(self):
        return BackendStatus(daemon=True, map=True, contacts=4)

    def threads(self):
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


def test_snapshot_converts_typed_client_models_for_qml():
    controller = BridgeController(
        backend=_Backend(),
        setup=object(),
        subscribe=False,
        autostart=False,
    )

    snapshot = controller._snapshot()

    assert snapshot["status"]["contacts"] == 4
    assert snapshot["threads"][0]["key"] == "address:email:test@example.com"


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
    controller._configured_mac = "02:00:00:00:00:01"

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
    controller._configured = True
    controller._target_saved = True
    controller._ancs_enabled = False
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
