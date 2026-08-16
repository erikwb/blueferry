"""Typed state passed between pairing orchestration stages."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from blueferry.bluetooth_devices import PairedDevice


class PairingAttempt(TypedDict, total=False):
    """Mutable diagnostic record assembled during one pairing attempt."""

    started_at: str
    blueferry: str
    blueferry_build: str
    blueferry_sha: str
    pairing_path: str
    session: dict[str, Any]
    _t0: float
    timeline: list[dict[str, Any]]
    bluez_trace: list[dict[str, Any]]
    previous_teardown: dict[str, object]
    controller: dict[str, Any]
    phone: dict[str, Any]
    daemon: dict[str, Any]
    pairing_policy: dict[str, Any]
    pairing_transaction: str
    preferred_bearer: str
    outcome: dict[str, object]


@dataclass(frozen=True, slots=True)
class PairingTransports:
    map: bool = False
    pbap: bool = False
    ancs: bool = False

    def as_tuple(self) -> tuple[bool, bool, bool]:
        return self.map, self.pbap, self.ancs


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    device: PairedDevice
    config: str
    service: str
    ancs: str
    ancs_enabled: bool
    transports: PairingTransports
    iphone_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "device": self.device.to_dict(),
            "config": self.config,
            "service": self.service,
            "ancs": self.ancs,
            "ancs_enabled": self.ancs_enabled,
            "ancs_ready": self.transports.ancs,
            "iphone_steps": list(self.iphone_steps),
        }
