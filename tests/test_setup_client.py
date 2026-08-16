"""The shared setup facade stays typed and delegates without touching BlueZ."""
from __future__ import annotations

import io
import json
import subprocess
import threading

import pytest

from blueferry import pair_setup, setup_client
from blueferry.pairing_types import PairingOutcome, PairingTransports


def _device() -> pair_setup.PairedDevice:
    return pair_setup.PairedDevice(
        mac="02:00:00:00:00:01",
        name="Test iPhone",
        icon="phone",
        trusted=True,
        connected=True,
        paired=True,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset({"test-uuid"}),
    )


def test_bluez_status_is_typed_and_serializable(monkeypatch):
    monkeypatch.setattr(
        setup_client.pair_setup,
        "bluez_support_status",
        lambda: {
            "active": True,
            "packaged_drop_in": True,
            "exec_start": "/usr/lib/bluetooth/bluetoothd --experimental",
        },
    )

    status = setup_client.SetupClient().bluez_status()

    assert status.active is True
    assert status.to_dict()["packaged_drop_in"] is True


def test_compatibility_and_configuration_are_typed(monkeypatch):
    monkeypatch.setattr(
        setup_client.pair_setup,
        "bluetooth_compatibility",
        lambda *_args, **_kwargs: {
            "adapter": "hci2",
            "available": True,
            "powered": True,
            "classic": True,
            "low_energy": True,
            "advertising": True,
            "secure_pairing": True,
            "hardware_supported": True,
            "messages_supported": True,
            "notifications_supported": True,
            "bearer_api_active": True,
            "pairing_ready": True,
            "issue": "",
            "supported_settings": ["br/edr", "le"],
        },
    )
    monkeypatch.setattr(
        setup_client.pair_setup,
        "configuration_status",
        lambda: {
            "configured": False,
            "mac": "",
            "adapter": "hci2",
            "path": "/tmp/local.env",
        },
    )
    monkeypatch.setattr(setup_client.quirks_report, "issue_report", lambda: None)

    client = setup_client.SetupClient()

    assert client.compatibility().pairing_ready is True
    assert client.compatibility().adapter == "hci2"
    assert client.configuration().configured is False
    assert client.configuration().pairing_issue_report == ""


def test_device_operations_delegate_with_requested_scan(monkeypatch):
    device = _device()
    calls = []
    monkeypatch.setattr(
        setup_client.pair_setup,
        "discover_devices",
        lambda seconds, adapter=None: calls.append((seconds, adapter)) or [device],
    )
    monkeypatch.setattr(
        setup_client.pair_setup,
        "list_devices",
        lambda: calls.append("list") or [device],
    )

    client = setup_client.SetupClient()

    assert client.devices(scan_seconds=8) == [device]
    assert client.devices(scan_seconds=8, adapter="hci1") == [device]
    assert client.devices() == [device]
    assert calls == [(8, None), (8, "hci1"), "list"]


def test_complete_pairing_preserves_typed_result_and_forget_delegates(monkeypatch):
    device = _device()
    forgotten = []
    paired = []
    monkeypatch.setattr(
        setup_client.pair_setup,
        "complete_pairing",
        lambda mac, **kwargs: paired.append((mac, kwargs.get("adapter")))
        or PairingOutcome(
            device=device,
            config="/tmp/test-config",
            service="restarted",
            ancs="connected",
            ancs_enabled=True,
            transports=PairingTransports(ancs=True),
            iphone_steps=("Enable notifications",),
        ),
    )
    monkeypatch.setattr(
        setup_client.pair_setup,
        "forget_device",
        lambda mac, **kwargs: forgotten.append((mac, kwargs.get("adapter"))),
    )

    client = setup_client.SetupClient()
    result = client.complete(
        device.mac,
        adapter="hci1",
        confirmation=lambda _passkey: True,
    )
    client.forget(device.mac, adapter="hci1")

    assert result.device.device_path == device.device_path
    assert result.iphone_steps == ("Enable notifications",)
    assert result.ancs_ready is True
    assert result.to_dict()["device"]["mac"] == device.mac
    assert PairingOutcome.from_dict(result.to_dict()) == result
    assert paired == [(device.mac, "hci1")]
    assert forgotten == [(device.mac, "hci1")]


def test_isolated_pairing_answers_helper_confirmation(monkeypatch):
    device = _device()
    commands = []

    class Input(io.StringIO):
        def flush(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = Input()
            self.stdout = iter([
                '{"event":"confirmation","passkey":"123456"}\n',
                '{"ok":true,"device":' + __import__("json").dumps(device.to_dict())
                + ',"ancs_ready":true}\n',
            ])
            self.stderr = io.StringIO()

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

        @staticmethod
        def poll():
            return 0

    process = Process()
    monkeypatch.setattr(
        setup_client.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or process,
    )

    result = setup_client.SetupClient().complete_isolated(
        device.mac,
        confirmation=lambda passkey: passkey == 123456,
        adapter="hci1",
        replace_saved_mac="02:00:00:00:00:02",
    )

    assert process.stdin.getvalue() == "yes\n"
    assert result.ancs_ready is True
    assert commands[0][-4:] == [
        "--adapter", "hci1", "--replace-saved-mac", "02:00:00:00:00:02",
    ]


def test_pairing_helper_timeout_resets_after_user_confirmation(monkeypatch):
    device = _device()

    class Process:
        stdin = io.StringIO()
        stdout = iter([
            '{"event":"confirmation","passkey":"123456"}\n',
            '{"ok":true,"device":' + json.dumps(device.to_dict()) + "}\n",
        ])
        stderr = io.StringIO()

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        setup_client.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(
        setup_client,
        "PAIRING_HELPER_IDLE_TIMEOUT_SECONDS",
        0.01,
    )

    def confirm(_passkey):
        threading.Event().wait(0.03)
        return True

    result = setup_client.SetupClient().complete_isolated(
        device.mac,
        confirmation=confirm,
    )

    assert result.device.mac == device.mac


def test_isolated_pairing_preserves_report_path_on_failure(monkeypatch):
    device = _device()

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = iter([
                json.dumps({
                    "ok": False,
                    "error": "Bluetooth confirmation did not complete",
                    "report_path": "/tmp/quirks-test.json",
                }) + "\n"
            ])
            self.stderr = io.StringIO()

        @staticmethod
        def wait(*_args, **_kwargs):
            return 2

        @staticmethod
        def poll():
            return 2

        @staticmethod
        def terminate():
            return None

    monkeypatch.setattr(
        setup_client.subprocess, "Popen", lambda *_args, **_kwargs: Process(),
    )

    with pytest.raises(setup_client.PairingError) as raised:
        setup_client.SetupClient().complete_isolated(
            device.mac,
            confirmation=lambda _passkey: True,
        )

    assert raised.value.report_path == "/tmp/quirks-test.json"
    assert str(raised.value) == "Bluetooth confirmation did not complete"


def test_isolated_pairing_adds_pairing_mode_helper_flags(monkeypatch):
    device = _device()
    commands = []

    class Process:
        stdin = io.StringIO()
        stdout = iter([
            '{"ok":true,"device":' + json.dumps(device.to_dict()) + "}\n",
        ])
        stderr = io.StringIO()

        @staticmethod
        def wait(*_args, **_kwargs):
            return 0

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        setup_client.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or Process(),
    )

    setup_client.SetupClient().complete_isolated(
        device.mac,
        confirmation=lambda _passkey: True,
        compatibility_mode=True,
        explicit_pairing=True,
    )

    assert "--compatibility-mode" in commands[0]
    assert commands[0][-1] == "--explicit-pairing"


def test_isolated_pairing_times_out_and_kills_a_stuck_helper(monkeypatch, caplog):
    device = _device()
    released = threading.Event()

    class BlockingOutput:
        def __iter__(self):
            return self

        def __next__(self):
            released.wait()
            raise StopIteration

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = BlockingOutput()
            self.stderr = io.StringIO("child diagnostic\n")
            self.running = True
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if self.running else -9

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.running = False
            released.set()

        def wait(self, timeout=None):
            if self.running:
                raise subprocess.TimeoutExpired("pairing-helper", timeout)
            return -9

    process = Process()
    monkeypatch.setattr(
        setup_client.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        setup_client,
        "PAIRING_HELPER_IDLE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(setup_client, "PAIRING_HELPER_STOP_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(setup_client.PairingError, match="Pairing helper timed out"):
        setup_client.SetupClient().complete_isolated(
            device.mac,
            confirmation=lambda _passkey: True,
        )

    assert process.terminated is True
    assert process.killed is True
    assert "child diagnostic" in caplog.text


def test_isolated_pairing_reports_exit_status_and_logs_bounded_stderr(
    monkeypatch,
    caplog,
):
    device = _device()
    diagnostic = "x" * (setup_client.PAIRING_HELPER_DIAGNOSTIC_CHARS + 20)

    class Process:
        stdin = io.StringIO()
        stdout = iter(())
        stderr = io.StringIO("discarded-prefix" + diagnostic)

        @staticmethod
        def wait(*_args, **_kwargs):
            return 7

        @staticmethod
        def poll():
            return 7

    monkeypatch.setattr(
        setup_client.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    with pytest.raises(
        setup_client.PairingError,
        match=r"Pairing helper exited without a result \(status 7\)",
    ):
        setup_client.SetupClient().complete_isolated(
            device.mac,
            confirmation=lambda _passkey: True,
        )

    assert "discarded-prefix" not in caplog.text
    assert diagnostic[-100:] in caplog.text
