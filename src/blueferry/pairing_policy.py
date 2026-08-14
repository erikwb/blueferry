"""Resolve the two supported iPhone pairing recipes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class PairingMode(str, Enum):
    """End-to-end behavior selected for one iPhone bond."""

    FULL = "full"
    COMPATIBILITY = "compatibility"


@dataclass(frozen=True, slots=True)
class PairingPolicy:
    """Concrete pairing and runtime decisions derived from capabilities."""

    mode: PairingMode
    pairing_strategy: str
    ancs_capable: bool
    ancs_enabled: bool
    solicitation_enabled: bool
    user_forced: bool
    reason: str

    @property
    def iphone_initiated(self) -> bool:
        return self.pairing_strategy == "iphone-initiated-connect"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["mode"] = self.mode.value
        return value


def resolve_pairing_policy(
    compatibility: Mapping[str, Any],
    *,
    force_compatibility: bool = False,
    force_explicit_pairing: bool = False,
    interactive: bool = True,
) -> PairingPolicy:
    """Choose full ANCS setup or the MAP/PBAP compatibility recipe.

    The short-lived ANCS solicitation is independent of connecting ANCS. It
    remains enabled whenever the adapter can advertise because older iOS uses
    it as the signal that exposes MAP/PBAP permissions.
    """
    ancs_capable = bool(compatibility.get("notifications_supported", False))
    solicitation_enabled = ancs_capable or bool(
        compatibility.get("low_energy", False)
        and compatibility.get("advertising", False)
    )
    compatibility_mode = force_compatibility or not ancs_capable
    if force_compatibility:
        reason = "user-override"
    elif not ancs_capable:
        reason = "ancs-unavailable"
    else:
        reason = "ancs-available"

    # Interactive transactions normally let the iPhone initiate
    # authentication, which is the reliable path for deriving the
    # cross-transport keys ANCS needs. A user-selected controller workaround
    # and headless callers instead start with explicit Pair.
    pairing_strategy = (
        "explicit-device-pair"
        if force_explicit_pairing or not interactive
        else "iphone-initiated-connect"
    )
    return PairingPolicy(
        mode=(
            PairingMode.COMPATIBILITY if compatibility_mode else PairingMode.FULL
        ),
        pairing_strategy=pairing_strategy,
        ancs_capable=ancs_capable,
        ancs_enabled=not compatibility_mode,
        solicitation_enabled=solicitation_enabled,
        user_forced=force_compatibility,
        reason=reason,
    )
