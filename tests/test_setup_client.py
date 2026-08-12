"""The shared setup facade stays typed and delegates without touching BlueZ."""
from __future__ import annotations

import io

from blueferry import pair_setup, setup_client


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
        lambda: {
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

    client = setup_client.SetupClient()

    assert client.compatibility().pairing_ready is True
    assert client.compatibility().adapter == "hci2"
    assert client.configuration().configured is False


def test_device_operations_delegate_with_requested_scan(monkeypatch):
    device = _device()
    calls = []
    monkeypatch.setattr(
        setup_client.pair_setup,
        "discover_devices",
        lambda seconds: calls.append(seconds) or [device],
    )
    monkeypatch.setattr(
        setup_client.pair_setup,
        "list_devices",
        lambda: calls.append("list") or [device],
    )

    client = setup_client.SetupClient()

    assert client.devices(scan_seconds=8) == [device]
    assert client.devices() == [device]
    assert calls == [8, "list"]


def test_complete_pairing_decodes_result_and_forget_delegates(monkeypatch):
    device = _device()
    forgotten = []
    monkeypatch.setattr(
        setup_client.pair_setup,
        "complete_pairing",
        lambda mac, **_kwargs: {
            "ok": True,
            "device": device.to_dict(),
            "config": "/tmp/test-config",
            "service": "restarted",
            "ancs": "connected",
            "ancs_ready": True,
            "iphone_steps": ["Enable notifications"],
        },
    )
    monkeypatch.setattr(
        setup_client.pair_setup,
        "forget_device",
        lambda mac: forgotten.append(mac),
    )

    client = setup_client.SetupClient()
    result = client.complete(device.mac)
    client.forget(device.mac)

    assert result.device.device_path == device.device_path
    assert result.iphone_steps == ("Enable notifications",)
    assert result.ancs_ready is True
    assert result.to_dict()["device"]["mac"] == device.mac
    assert forgotten == [device.mac]


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
        replace_saved_mac="02:00:00:00:00:02",
    )

    assert process.stdin.getvalue() == "yes\n"
    assert result.ancs_ready is True
    assert commands[0][-2:] == ["--replace-saved-mac", "02:00:00:00:00:02"]
