"""Toolkit-neutral first-run state derivation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from blueferry.models import BackendStatus
from blueferry.setup_verification import remaining_iphone_setup_tasks


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
    status: Mapping | BackendStatus,
) -> OnboardingStage:
    """Return the user-facing setup stage from observable state only."""
    status_values = status.to_dict() if isinstance(status, BackendStatus) else status
    if not setup_loaded:
        return OnboardingStage.CHECKING
    if not compatibility.get("hardware_supported"):
        return OnboardingStage.INCOMPATIBLE
    if compatibility.get("notifications_supported") and not compatibility.get("bearer_api_active"):
        return OnboardingStage.ACTIVATE_BLUETOOTH
    if not configured:
        return OnboardingStage.SELECT_DEVICE
    remaining_tasks = remaining_iphone_setup_tasks(
        status_values.get("verified_iphone_setup", ()),
        notifications_supported=bool(compatibility.get("notifications_supported")),
    )
    if status_values.get("map") and status_values.get("pbap"):
        if remaining_tasks:
            return OnboardingStage.IPHONE_SETTINGS
        if not compatibility.get("notifications_supported"):
            return OnboardingStage.READY_WITHOUT_ANCS
        return OnboardingStage.READY
    if status_values.get("daemon"):
        return OnboardingStage.IPHONE_SETTINGS
    return OnboardingStage.STARTING
