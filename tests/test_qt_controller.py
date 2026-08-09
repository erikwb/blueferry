"""Kirigami presentation state is built from typed clients without live I/O."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from blueferry.models import BackendStatus, Thread
from blueferry.qt.controller import BridgeController


class _Backend:
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
