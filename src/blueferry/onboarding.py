"""Toolkit-neutral first-run state derivation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from blueferry.models import BackendStatus
from blueferry.setup_verification import remaining_iphone_setup_tasks


class CompatibilityState(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


class ConfigurationState(Protocol):
    configured: bool
    ancs_enabled: bool


class OnboardingStage(str, Enum):
    CHECKING = "checking"
    ACTIVATE_BLUETOOTH = "activate-bluetooth"
    SELECT_DEVICE = "select-device"
    STARTING = "starting"
    IPHONE_SETTINGS = "iphone-settings"
    READY = "ready"
    READY_WITHOUT_ANCS = "ready-without-ancs"

    def __str__(self) -> str:
        return self.value


_READY_STAGES = {
    OnboardingStage.READY,
    OnboardingStage.READY_WITHOUT_ANCS,
}


@dataclass(frozen=True, slots=True)
class OnboardingTransition:
    previous: OnboardingStage
    current: OnboardingStage

    @property
    def became_ready(self) -> bool:
        return self.current in _READY_STAGES and self.previous not in _READY_STAGES


class OnboardingState:
    """Reduce typed setup and daemon snapshots to one presentation stage."""

    def __init__(self) -> None:
        self.setup_loaded = False
        self.compatibility: dict[str, Any] = {}
        self.configuration: ConfigurationState | None = None
        self.status = BackendStatus()
        self.compatibility_mode = False
        self.stage = OnboardingStage.CHECKING

    def update(
        self,
        *,
        setup_loaded: bool,
        compatibility: CompatibilityState | Mapping[str, Any] | None,
        configuration: ConfigurationState | None,
        status: BackendStatus,
        compatibility_mode: bool = False,
    ) -> OnboardingTransition:
        previous = self.stage
        self.setup_loaded = setup_loaded
        if isinstance(compatibility, Mapping):
            self.compatibility = dict(compatibility)
        elif compatibility is not None:
            self.compatibility = compatibility.to_dict()
        else:
            self.compatibility = {}
        self.configuration = configuration
        self.status = status
        self.compatibility_mode = compatibility_mode
        effective = dict(self.compatibility)
        if compatibility_mode or (
            configuration is not None
            and configuration.configured
            and not configuration.ancs_enabled
        ):
            effective["notifications_supported"] = False
        self.stage = derive_stage(
            setup_loaded=setup_loaded,
            configured=bool(configuration and configuration.configured),
            compatibility=effective,
            status=status,
        )
        return OnboardingTransition(previous, self.stage)


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
