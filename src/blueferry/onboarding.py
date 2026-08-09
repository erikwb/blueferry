"""Toolkit-neutral first-run state derivation."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


class OnboardingStage(str, Enum):
    CHECKING = "checking"
    INCOMPATIBLE = "incompatible"
    ACTIVATE_BLUETOOTH = "activate-bluetooth"
    SELECT_DEVICE = "select-device"
    STARTING = "starting"
    IPHONE_SETTINGS = "iphone-settings"
    READY = "ready"
    READY_WITHOUT_ANCS = "ready-without-ancs"

    def __str__(self) -> str:
        return self.value


def derive_stage(
    *,
    setup_loaded: bool,
    configured: bool,
    compatibility: Mapping,
    status: Mapping,
) -> OnboardingStage:
    """Return the user-facing setup stage from observable state only."""
    if not setup_loaded:
        return OnboardingStage.CHECKING
    if not compatibility.get("hardware_supported"):
        return OnboardingStage.INCOMPATIBLE
    if (
        compatibility.get("notifications_supported")
        and not compatibility.get("bearer_api_active")
    ):
        return OnboardingStage.ACTIVATE_BLUETOOTH
    if not configured:
        return OnboardingStage.SELECT_DEVICE
    if status.get("map") and status.get("pbap"):
        if status.get("ancs"):
            return OnboardingStage.READY
        if not compatibility.get("notifications_supported"):
            return OnboardingStage.READY_WITHOUT_ANCS
        return OnboardingStage.IPHONE_SETTINGS
    if status.get("daemon"):
        return OnboardingStage.IPHONE_SETTINGS
    return OnboardingStage.STARTING
