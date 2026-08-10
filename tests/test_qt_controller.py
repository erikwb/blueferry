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


def test_pairing_uses_interactive_agent_and_accepts_matching_code(monkeypatch):
    observed = []

    class Setup:
        def complete(self, mac, *, confirmation, display):
            observed.append((mac, confirmation(12345)))
            display(12345)
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
    passkeys = []

    def confirm(passkey):
        passkeys.append(passkey)
        controller.answerPairingConfirmation(True)

    controller.pairingConfirmationRequested.connect(confirm)

    controller.completePairing("02:00:00:00:00:01")

    assert passkeys == ["012345"]
    assert observed == [("02:00:00:00:00:01", True)]


def test_pairing_rejects_when_confirmation_is_declined(monkeypatch):
    observed = []

    class Setup:
        def complete(self, _mac, *, confirmation, display):
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
