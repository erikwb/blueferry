"""Typed setup API shared by native clients and JSON command adapters."""
from __future__ import annotations

import json

# This fixed, shell-free invocation launches BlueFerry's own helper module.
import subprocess  # nosec B404
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from blueferry import pair_setup, quirks_report
from blueferry.bluetooth_devices import PairedDevice
from blueferry.errors import PairingError


@dataclass(frozen=True, slots=True)
class BluezSupport:
    active: bool
    packaged_drop_in: bool
    exec_start: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BluezSupport:
        return cls(
            active=bool(value.get("active", False)),
            packaged_drop_in=bool(value.get("packaged_drop_in", False)),
            exec_start=str(value.get("exec_start", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "packaged_drop_in": self.packaged_drop_in,
            "exec_start": self.exec_start,
        }


@dataclass(frozen=True, slots=True)
class BluetoothCompatibility:
    adapter: str
    available: bool
    powered: bool
    classic: bool
    low_energy: bool
    advertising: bool
    secure_pairing: bool
    hardware_supported: bool
    messages_supported: bool
    notifications_supported: bool
    bearer_api_active: bool
    pairing_ready: bool
    issue: str
    supported_settings: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BluetoothCompatibility:
        return cls(
            adapter=str(value.get("adapter", "")),
            available=bool(value.get("available", False)),
            powered=bool(value.get("powered", False)),
            classic=bool(value.get("classic", False)),
            low_energy=bool(value.get("low_energy", False)),
            advertising=bool(value.get("advertising", False)),
            secure_pairing=bool(value.get("secure_pairing", False)),
            hardware_supported=bool(value.get("hardware_supported", False)),
            messages_supported=bool(value.get("messages_supported", False)),
            notifications_supported=bool(value.get("notifications_supported", False)),
            bearer_api_active=bool(value.get("bearer_api_active", False)),
            pairing_ready=bool(value.get("pairing_ready", False)),
            issue=str(value.get("issue", "")),
            supported_settings=tuple(
                str(item) for item in value.get("supported_settings", ())
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        } | {"supported_settings": list(self.supported_settings)}


@dataclass(frozen=True, slots=True)
class ConfigurationState:
    configured: bool
    mac: str
    adapter: str
    path: str
    saved: bool = False
    bonded: bool | None = None
    pairing_issue_report: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConfigurationState:
        return cls(
            configured=bool(value.get("configured", False)),
            mac=str(value.get("mac", "")),
            adapter=str(value.get("adapter", "")),
            path=str(value.get("path", "")),
            saved=bool(value.get("saved", value.get("configured", False))),
            bonded=(
                value.get("bonded")
                if isinstance(value.get("bonded"), bool)
                else None
            ),
            pairing_issue_report=str(value.get("pairing_issue_report", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mac": self.mac,
            "adapter": self.adapter,
            "path": self.path,
            "saved": self.saved,
            "bonded": self.bonded,
            "pairing_issue_report": self.pairing_issue_report,
        }


@dataclass(frozen=True, slots=True)
class PairingResult:
    ok: bool
    device: PairedDevice
    config: str
    service: str
    ancs: str
    ancs_ready: bool
    iphone_steps: tuple[str, ...]
    quirks_report: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PairingResult:
        raw_device = value.get("device", {})
        if not isinstance(raw_device, dict):
            raw_device = {}
        raw_steps = value.get("iphone_steps", ())
        return cls(
            ok=bool(value.get("ok", False)),
            device=PairedDevice.from_dict(raw_device),
            config=str(value.get("config", "")),
            service=str(value.get("service", "")),
            ancs=str(value.get("ancs", "")),
            ancs_ready=bool(value.get("ancs_ready", False)),
            iphone_steps=tuple(str(item) for item in raw_steps)
            if isinstance(raw_steps, list | tuple) else (),
            quirks_report=str(value.get("quirks_report", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "device": self.device.to_dict(),
            "config": self.config,
            "service": self.service,
            "ancs": self.ancs,
            "ancs_ready": self.ancs_ready,
            "iphone_steps": list(self.iphone_steps),
            "quirks_report": self.quirks_report,
        }


class SetupClient:
    """Direct Python setup facade; operations may block and belong off the UI thread."""

    def bluez_status(self) -> BluezSupport:
        return BluezSupport.from_dict(pair_setup.bluez_support_status())

    def compatibility(self) -> BluetoothCompatibility:
        return BluetoothCompatibility.from_dict(
            pair_setup.bluetooth_compatibility()
        )

    def configuration(self) -> ConfigurationState:
        value = dict(pair_setup.configuration_status())
        report = quirks_report.issue_report()
        value["pairing_issue_report"] = str(report) if report is not None else ""
        return ConfigurationState.from_dict(value)

    def activate_bluez(self) -> BluezSupport:
        return BluezSupport.from_dict(pair_setup.activate_bluez_support())

    def devices(self, *, scan_seconds: int = 0) -> list[PairedDevice]:
        if scan_seconds:
            return pair_setup.discover_devices(scan_seconds)
        return pair_setup.list_devices()

    def complete(
        self,
        mac: str,
        *,
        confirmation: pair_setup.ConfirmationCallback | None = None,
        display: pair_setup.DisplayCallback | None = None,
    ) -> PairingResult:
        return PairingResult.from_dict(
            pair_setup.complete_pairing(
                mac,
                confirmation=confirmation,
                display=display,
            )
        )

    def complete_isolated(
        self,
        mac: str,
        *,
        confirmation: pair_setup.ConfirmationCallback,
        display: pair_setup.DisplayCallback | None = None,
        replace_saved_mac: str = "",
    ) -> PairingResult:
        """Run interactive pairing in a D-Bus/GLib-isolated child process."""
        command = [
            sys.executable,
            "-m",
            "blueferry",
            "pairing-complete",
            mac,
            "--interactive-agent",
        ]
        if replace_saved_mac:
            command.extend(["--replace-saved-mac", replace_saved_mac])
        process = subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise PairingError("Could not open the pairing helper pipes")
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "confirmation":
                    raw = str(event.get("passkey", ""))
                    accepted = confirmation(int(raw) if raw else None)
                    process.stdin.write("yes\n" if accepted else "no\n")
                    process.stdin.flush()
                elif event.get("event") == "display":
                    if display is not None:
                        display(int(str(event["passkey"])))
                elif event.get("ok") is True:
                    process.wait()
                    return PairingResult.from_dict(event)
                elif event.get("ok") is False:
                    path = str(event.get("report_path") or "").strip()
                    raise PairingError(
                        str(event.get("error", "Pairing failed")),
                        report_path=path or None,
                    )
            raise PairingError("Pairing helper exited without a result")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def forget(self, mac: str) -> None:
        pair_setup.forget_device(mac)

    def prepare_replacement(self, previous_mac: str, next_mac: str) -> None:
        pair_setup.prepare_target_replacement(previous_mac, next_mac)
