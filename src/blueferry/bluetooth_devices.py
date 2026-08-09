"""Typed BlueZ device projection shared by setup workflows and clients."""
from __future__ import annotations

from dataclasses import dataclass

from blueferry import config


@dataclass(slots=True)
class PairedDevice:
    mac: str
    name: str
    icon: str
    trusted: bool
    connected: bool
    paired: bool
    adapter_path: str
    device_path: str
    uuids: frozenset[str]
    services_resolved: bool = False

    @property
    def likely_iphone(self) -> bool:
        icon = (self.icon or "").casefold()
        name = (self.name or "").casefold()
        return (
            icon in {"phone", "smartphone", "phone-apple-iphone"}
            or "iphone" in name
            or "ipad" in name
        )

    @property
    def ancs_bonded(self) -> bool:
        return config.ANCS_SOLICIT_UUID.casefold() in {
            item.casefold() for item in self.uuids
        }

    @classmethod
    def from_dict(cls, value: dict) -> PairedDevice:
        return cls(
            mac=str(value.get("mac", "")),
            name=str(value.get("name", "(unnamed)")),
            icon=str(value.get("icon", "")),
            trusted=bool(value.get("trusted", False)),
            connected=bool(value.get("connected", False)),
            paired=bool(value.get("paired", False)),
            adapter_path=str(value.get("adapter_path", "")),
            device_path=str(value.get("device_path", "")),
            uuids=frozenset(str(item) for item in value.get("uuids", [])),
            services_resolved=bool(value.get("services_resolved", False)),
        )

    def to_dict(self) -> dict:
        return {
            "mac": self.mac,
            "name": self.name,
            "icon": self.icon,
            "trusted": self.trusted,
            "connected": self.connected,
            "paired": self.paired,
            "likely_iphone": self.likely_iphone,
            "ancs_bonded": self.ancs_bonded,
            "adapter_path": self.adapter_path,
            "device_path": self.device_path,
            "uuids": sorted(self.uuids),
            "services_resolved": self.services_resolved,
        }
