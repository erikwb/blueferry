"""Typed state passed between pairing orchestration stages."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    _aliases: list[str]
    timeline: list[dict[str, Any]]
    bluez_trace: list[dict[str, Any]]
    previous_teardown: dict[str, object]
    controller: dict[str, Any]
    phone: dict[str, Any]
    daemon: dict[str, Any]
    pairing_options: dict[str, bool]
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
    quirks_report: str = ""

    @property
    def ancs_ready(self) -> bool:
        return self.transports.ancs

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PairingOutcome:
        raw_device = value.get("device", {})
        if not isinstance(raw_device, Mapping):
            raw_device = {}
        raw_steps = value.get("iphone_steps", ())
        return cls(
            device=PairedDevice.from_dict(dict(raw_device)),
            config=str(value.get("config", "")),
            service=str(value.get("service", "")),
            ancs=str(value.get("ancs", "")),
            ancs_enabled=bool(value.get("ancs_enabled", True)),
            transports=PairingTransports(
                map=bool(value.get("map_ready", False)),
                pbap=bool(value.get("pbap_ready", False)),
                ancs=bool(value.get("ancs_ready", False)),
            ),
            iphone_steps=tuple(str(item) for item in raw_steps)
            if isinstance(raw_steps, list | tuple)
            else (),
            quirks_report=str(value.get("quirks_report", "")),
        )

    def with_quirks_report(self, path: str) -> PairingOutcome:
        return replace(self, quirks_report=path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "device": self.device.to_dict(),
            "config": self.config,
            "service": self.service,
            "ancs": self.ancs,
            "ancs_enabled": self.ancs_enabled,
            "map_ready": self.transports.map,
            "pbap_ready": self.transports.pbap,
            "ancs_ready": self.transports.ancs,
            "iphone_steps": list(self.iphone_steps),
            "quirks_report": self.quirks_report,
        }
