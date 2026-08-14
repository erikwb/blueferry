"""Pairing policy keeps ANCS capability separate from solicitation."""

from blueferry.pairing_policy import PairingMode, resolve_pairing_policy


def _capabilities(*, notifications: bool = True) -> dict[str, bool]:
    return {
        "notifications_supported": notifications,
        "low_energy": True,
        "advertising": True,
    }


def test_ancs_capable_adapter_lets_the_iphone_initiate() -> None:
    policy = resolve_pairing_policy(_capabilities())

    assert policy.mode is PairingMode.FULL
    assert policy.pairing_strategy == "iphone-initiated-connect"
    assert policy.ancs_enabled is True
    assert policy.solicitation_enabled is True
    assert policy.reason == "ancs-available"


def test_user_override_keeps_connect_first_in_compatibility_mode() -> None:
    policy = resolve_pairing_policy(
        _capabilities(),
        force_compatibility=True,
    )

    assert policy.mode is PairingMode.COMPATIBILITY
    assert policy.pairing_strategy == "iphone-initiated-connect"
    assert policy.ancs_enabled is False
    assert policy.solicitation_enabled is True
    assert policy.user_forced is True


def test_missing_ancs_capability_selects_compatibility_but_keeps_soliciting() -> None:
    policy = resolve_pairing_policy(_capabilities(notifications=False))

    assert policy.mode is PairingMode.COMPATIBILITY
    assert policy.pairing_strategy == "iphone-initiated-connect"
    assert policy.ancs_enabled is False
    assert policy.solicitation_enabled is True
    assert policy.reason == "ancs-unavailable"


def test_adapter_without_le_advertising_cannot_solicit() -> None:
    policy = resolve_pairing_policy({
        "notifications_supported": False,
        "low_energy": False,
        "advertising": False,
    })

    assert policy.mode is PairingMode.COMPATIBILITY
    assert policy.solicitation_enabled is False


def test_headless_compatibility_uses_explicit_pair_fallback() -> None:
    policy = resolve_pairing_policy(
        _capabilities(notifications=False),
        interactive=False,
    )

    assert policy.mode is PairingMode.COMPATIBILITY
    assert policy.pairing_strategy == "explicit-device-pair"
    assert policy.ancs_enabled is False


def test_user_can_force_explicit_pairing_independently_of_compatibility() -> None:
    policy = resolve_pairing_policy(
        _capabilities(),
        force_explicit_pairing=True,
    )

    assert policy.mode is PairingMode.FULL
    assert policy.pairing_strategy == "explicit-device-pair"
    assert policy.ancs_enabled is True
