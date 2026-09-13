"""Typed setup API shared by native clients and JSON command adapters."""
from __future__ import annotations

import json
import logging
import queue

# This fixed, shell-free invocation launches BlueFerry's own helper module.
import subprocess  # nosec B404
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any, cast

from blueferry import pair_setup, quirks_report
from blueferry.bluetooth_devices import PairedDevice
from blueferry.errors import PairingError
from blueferry.pairing_types import PairingOutcome

DISCOVERY_SECONDS = pair_setup.DISCOVERY_SECONDS
PAIRING_HELPER_IDLE_TIMEOUT_SECONDS = 300.0
PAIRING_HELPER_STOP_TIMEOUT_SECONDS = 5.0
PAIRING_HELPER_DIAGNOSTIC_CHARS = 4096

log = logging.getLogger(__name__)

_PAIRING_HELPER_EOF = object()


def _close_helper_pipe(stream: IO[str] | None) -> None:
    if stream is not None:
        try:
            stream.close()
        except OSError:
            # Closing stdin flushes it again, including after a failed write.
            # The pipe still closes; do not replace the original pairing error.
            log.debug("Could not flush a pairing helper pipe on close", exc_info=True)


class _BoundedDiagnostics:
    """Keep a thread-safe tail of child output without unbounded buffering."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._text = ""
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        with self._lock:
            self._text = (self._text + text)[-self._limit :]

    def text(self) -> str:
        with self._lock:
            return self._text.strip()


def _read_helper_stdout(
    stream: IO[str],
    pending: queue.Queue[object],
    stopped: threading.Event,
) -> None:
    def publish(item: object) -> bool:
        while not stopped.is_set():
            try:
                pending.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    try:
        for line in stream:
            if not publish(line):
                return
    except Exception as exc:  # pragma: no cover - OS pipe errors are timing-specific
        publish(exc)
    finally:
        _close_helper_pipe(stream)
        publish(_PAIRING_HELPER_EOF)


def _read_helper_stderr(stream: IO[str], diagnostics: _BoundedDiagnostics) -> None:
    try:
        while chunk := stream.read(1024):
            diagnostics.append(chunk)
    except Exception:  # pragma: no cover - diagnostics must never mask pairing
        return
    finally:
        _close_helper_pipe(stream)


def _drain_diagnostics(
    diagnostics: _BoundedDiagnostics,
    diagnostics_thread: threading.Thread | None,
) -> str:
    if diagnostics_thread is not None:
        diagnostics_thread.join(timeout=0.25)
    return diagnostics.text()


class _HelperOutput:
    """Bounded stdout reader that can stop even when its queue is full."""

    def __init__(self, stream: IO[str]) -> None:
        self._pending: queue.Queue[object] = queue.Queue(maxsize=64)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=_read_helper_stdout,
            args=(stream, self._pending, self._stopped),
            name="blueferry-pairing-output",
            daemon=True,
        )
        self._thread.start()

    def read(self, timeout: float) -> str | None:
        try:
            item = self._pending.get(timeout=timeout)
        except queue.Empty as exc:
            raise PairingError("Pairing helper timed out") from exc
        if item is _PAIRING_HELPER_EOF:
            return None
        if isinstance(item, Exception):
            raise PairingError("Could not read from the pairing helper") from item
        return cast(str, item)

    def close(self) -> None:
        self._stopped.set()
        # The reader owns stdout and closes it itself. Closing a TextIOWrapper
        # on this thread could block on the reader's lock during an OS read.
        self._thread.join(timeout=0.25)


def _wait_for_helper(
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PairingError("Pairing helper timed out") from exc


def _stop_helper(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=PAIRING_HELPER_STOP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        process.wait(timeout=PAIRING_HELPER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.error("Pairing helper did not exit after being killed")


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
class AdapterOption:
    name: str
    label: str
    available: bool = False
    powered: bool = False
    hardware_supported: bool = False
    notifications_supported: bool = False
    pairing_ready: bool = False
    issue: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterOption:
        return cls(
            name=str(value.get("name", "")),
            label=str(value.get("label") or value.get("name") or ""),
            available=bool(value.get("available", False)),
            powered=bool(value.get("powered", False)),
            hardware_supported=bool(value.get("hardware_supported", False)),
            notifications_supported=bool(value.get("notifications_supported", False)),
            pairing_ready=bool(value.get("pairing_ready", False)),
            issue=str(value.get("issue", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available,
            "powered": self.powered,
            "hardware_supported": self.hardware_supported,
            "notifications_supported": self.notifications_supported,
            "pairing_ready": self.pairing_ready,
            "issue": self.issue,
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
    adapters: tuple[AdapterOption, ...] = ()
    controller_vendor: str = ""
    ancs_limited_controller: bool = False
    explicit_pairing_default: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BluetoothCompatibility:
        raw_adapters = value.get("adapters", ())
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
            adapters=tuple(
                AdapterOption.from_dict(item)
                for item in raw_adapters
                if isinstance(item, dict)
            ),
            controller_vendor=str(value.get("controller_vendor") or ""),
            ancs_limited_controller=bool(value.get("ancs_limited_controller")),
            explicit_pairing_default=bool(value.get("explicit_pairing_default", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        } | {
            "supported_settings": list(self.supported_settings),
            "adapters": [item.to_dict() for item in self.adapters],
        }


@dataclass(frozen=True, slots=True)
class ConfigurationState:
    configured: bool
    mac: str
    adapter: str
    path: str
    saved: bool = False
    bonded: bool | None = None
    pairing_issue_report: str = ""
    ancs_enabled: bool = True

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
            ancs_enabled=bool(value.get("ancs_enabled", True)),
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
            "ancs_enabled": self.ancs_enabled,
        }


class SetupClient:
    """Direct Python setup facade; operations may block and belong off the UI thread."""

    def bluez_status(self) -> BluezSupport:
        return BluezSupport.from_dict(pair_setup.bluez_support_status())

    def compatibility(self, adapter: str | None = None) -> BluetoothCompatibility:
        return BluetoothCompatibility.from_dict(
            pair_setup.bluetooth_compatibility(adapter)
        )

    def configuration(self) -> ConfigurationState:
        value = dict(pair_setup.configuration_status())
        report = quirks_report.issue_report()
        value["pairing_issue_report"] = str(report) if report is not None else ""
        return ConfigurationState.from_dict(value)

    def activate_bluez(self) -> BluezSupport:
        return BluezSupport.from_dict(pair_setup.activate_bluez_support())

    def devices(
        self, *, scan_seconds: int = 0, adapter: str | None = None,
    ) -> list[PairedDevice]:
        if scan_seconds:
            return pair_setup.discover_devices(scan_seconds, adapter=adapter)
        return pair_setup.list_devices()

    def complete(
        self,
        mac: str,
        *,
        confirmation: pair_setup.ConfirmationCallback,
        adapter: str | None = None,
        display: pair_setup.DisplayCallback | None = None,
        compatibility_mode: bool = False,
        explicit_pairing: bool = False,
        transports_changed: pair_setup.TransportCallback | None = None,
    ) -> PairingOutcome:
        return pair_setup.complete_pairing(
            mac,
            adapter=adapter,
            confirmation=confirmation,
            display=display,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
            transports_changed=transports_changed,
        )

    def complete_isolated(
        self,
        mac: str,
        *,
        confirmation: pair_setup.ConfirmationCallback,
        display: pair_setup.DisplayCallback | None = None,
        adapter: str | None = None,
        replace_saved_mac: str = "",
        compatibility_mode: bool = False,
        explicit_pairing: bool = False,
    ) -> PairingOutcome:
        """Run interactive pairing in a D-Bus/GLib-isolated child process."""
        command = [
            sys.executable,
            "-m",
            "blueferry",
            "pairing-complete",
            mac,
            "--interactive-agent",
        ]
        if adapter:
            command.extend(["--adapter", adapter])
        if replace_saved_mac:
            command.extend(["--replace-saved-mac", replace_saved_mac])
        if compatibility_mode:
            command.append("--compatibility-mode")
        if explicit_pairing:
            command.append("--explicit-pairing")
        process = subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            _stop_helper(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                _close_helper_pipe(stream)
            raise PairingError("Could not open the pairing helper pipes")
        diagnostics = _BoundedDiagnostics(PAIRING_HELPER_DIAGNOSTIC_CHARS)
        diagnostics_thread: threading.Thread | None = None
        if process.stderr is not None:
            diagnostics_thread = threading.Thread(
                target=_read_helper_stderr,
                args=(process.stderr, diagnostics),
                name="blueferry-pairing-diagnostics",
                daemon=True,
            )
            diagnostics_thread.start()
        output = _HelperOutput(process.stdout)
        confirmation_deadline: float | None = None

        def remaining_timeout() -> float:
            if confirmation_deadline is None:
                return PAIRING_HELPER_IDLE_TIMEOUT_SECONDS
            return max(0.0, confirmation_deadline - time.monotonic())

        failed = False
        try:
            while True:
                timeout = remaining_timeout()
                if confirmation_deadline is not None and timeout <= 0:
                    raise PairingError("Pairing helper stopped accepting confirmation")
                try:
                    line = output.read(timeout)
                except PairingError as error:
                    if confirmation_deadline is not None:
                        raise PairingError(
                            "Pairing helper stopped accepting confirmation"
                        ) from error
                    raise
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "confirmation":
                    if confirmation_deadline is not None:
                        continue
                    raw = str(event.get("passkey", ""))
                    accepted = confirmation(int(raw) if raw else None)
                    try:
                        process.stdin.write("yes\n" if accepted else "no\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        _close_helper_pipe(process.stdin)
                        # The helper may have failed while the dialog was open.
                        # Drain its queued result to preserve the error/report,
                        # but give a broken protocol only a bounded grace period.
                        confirmation_deadline = (
                            time.monotonic() + PAIRING_HELPER_STOP_TIMEOUT_SECONDS
                        )
                elif event.get("event") == "display":
                    if display is not None:
                        display(int(str(event["passkey"])))
                elif event.get("ok") is True:
                    status = _wait_for_helper(
                        process,
                        timeout=remaining_timeout(),
                    )
                    if status != 0:
                        detail = _drain_diagnostics(diagnostics, diagnostics_thread)
                        message = f"Pairing helper exited unexpectedly (status {status})"
                        if detail:
                            message = f"{message}: {detail}"
                        raise PairingError(message)
                    if confirmation_deadline is not None:
                        raise PairingError("Could not deliver pairing confirmation to the helper")
                    return PairingOutcome.from_dict(event)
                elif event.get("ok") is False:
                    path = str(event.get("report_path") or "").strip()
                    raise PairingError(
                        str(event.get("error", "Pairing failed")),
                        report_path=path or None,
                    )
            status = _wait_for_helper(
                process,
                timeout=remaining_timeout(),
            )
            detail = _drain_diagnostics(diagnostics, diagnostics_thread)
            message = f"Pairing helper exited without a result (status {status})"
            if detail:
                message = f"{message}: {detail}"
            raise PairingError(message)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                _stop_helper(process)
            finally:
                _close_helper_pipe(process.stdin)
                output.close()
                if diagnostics_thread is not None:
                    diagnostics_thread.join(timeout=0.25)
                if failed and (details := diagnostics.text()):
                    log.warning("Pairing helper diagnostics (bounded tail): %s", details)

    def forget(self, mac: str, *, adapter: str | None = None) -> None:
        pair_setup.forget_device(mac, adapter=adapter)

    def prepare_replacement(
        self, previous_mac: str, next_mac: str, *, adapter: str | None = None,
    ) -> None:
        pair_setup.prepare_target_replacement(
            previous_mac, next_mac, adapter=adapter,
        )
