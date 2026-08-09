"""First-run stages are derived without accessing Bluetooth."""

from __future__ import annotations

from blueferry.models import BackendStatus
from blueferry.onboarding import OnboardingStage, derive_stage

COMPATIBLE = {
    "hardware_supported": True,
    "notifications_supported": True,
    "bearer_api_active": True,
}


def test_unconfigured_compatible_install_requests_a_device() -> None:
    assert (
        derive_stage(
            setup_loaded=True,
            configured=False,
            compatibility=COMPATIBLE,
            status={},
        )
        is OnboardingStage.SELECT_DEVICE
    )


def test_optional_ancs_transport_controls_activation_step() -> None:
    inactive = {**COMPATIBLE, "bearer_api_active": False}
    assert (
        derive_stage(
            setup_loaded=True,
            configured=False,
            compatibility=inactive,
            status={},
        )
        is OnboardingStage.ACTIVATE_BLUETOOTH
    )
    assert (
        derive_stage(
            setup_loaded=True,
            configured=False,
            compatibility={
                **inactive,
                "notifications_supported": False,
                "pairing_ready": True,
            },
            status={},
        )
        is OnboardingStage.SELECT_DEVICE
    )


def test_profiles_are_success_boundary_and_ancs_is_capability_aware() -> None:
    base = dict(
        setup_loaded=True,
        configured=True,
        compatibility=COMPATIBLE,
    )
    assert (
        derive_stage(**base, status={"daemon": True, "map": False})
        is OnboardingStage.IPHONE_SETTINGS
    )
    assert (
        derive_stage(**base, status={"daemon": True, "map": True, "pbap": True})
        is OnboardingStage.IPHONE_SETTINGS
    )
    assert (
        derive_stage(
            **base,
            status={
                "daemon": True,
                "map": True,
                "pbap": True,
                "ancs": True,
                "verified_iphone_setup": [
                    "message-notifications",
                    "contacts",
                    "notification-access",
                ],
            },
        )
        is OnboardingStage.READY
    )
    assert (
        derive_stage(
            **{
                **base,
                "compatibility": {
                    **COMPATIBLE,
                    "notifications_supported": False,
                },
            },
            status={
                "daemon": True,
                "map": True,
                "pbap": True,
                "verified_iphone_setup": ["message-notifications", "contacts"],
            },
        )
        is OnboardingStage.READY_WITHOUT_ANCS
    )


def test_typed_backend_status_is_accepted_by_native_clients() -> None:
    assert (
        derive_stage(
            setup_loaded=True,
            configured=True,
            compatibility=COMPATIBLE,
            status=BackendStatus(
                daemon=True,
                map=True,
                pbap=True,
                ancs=True,
                verified_iphone_setup=(
                    "message-notifications",
                    "contacts",
                    "notification-access",
                ),
            ),
        )
        is OnboardingStage.READY
    )


def test_connected_profiles_still_request_unverified_iphone_steps() -> None:
    assert (
        derive_stage(
            setup_loaded=True,
            configured=True,
            compatibility=COMPATIBLE,
            status={
                "daemon": True,
                "map": True,
                "pbap": True,
                "ancs": True,
                "verified_iphone_setup": ["contacts", "notification-access"],
            },
        )
        is OnboardingStage.IPHONE_SETTINGS
    )
