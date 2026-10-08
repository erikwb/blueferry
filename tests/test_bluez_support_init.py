"""BlueZ experimental-mode detection and guidance outside systemd."""
from __future__ import annotations

import pytest

from blueferry import pair_setup, service_manager
from blueferry.bluetooth_capabilities import (
    OPENRC_BLUEZ_ACTIVATION_HINT,
    UNMANAGED_BLUEZ_ACTIVATION_HINT,
    activate_bluez_support,
    bluez_support_status,
)
from blueferry.errors import PairingError
from blueferry.onboarding import ancs_unavailable_detail
from blueferry.pairing_cli import _print_ancs_repair_hint
from blueferry.setup_client import BluetoothCompatibility, BluezSupport


def _process(proc, pid: int, comm: str, argv: list[bytes]) -> None:
    entry = proc / str(pid)
    entry.mkdir()
    entry.joinpath("comm").write_text(comm + "\n")
    entry.joinpath("cmdline").write_bytes(b"\0".join(argv) + b"\0")


def _no_commands(*_args, **_kwargs):
    raise AssertionError("init-agnostic BlueZ detection must not run commands")


@pytest.fixture
def openrc(monkeypatch):
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)


@pytest.mark.parametrize("flag", [b"-E", b"--experimental"])
def test_openrc_detects_experimental_bluetoothd_from_proc(tmp_path, openrc, flag):
    _process(tmp_path, 12, "bash", [b"/bin/bash", b"-E"])
    _process(tmp_path, 812, "bluetoothd", [b"/usr/libexec/bluetooth/bluetoothd", flag])
    (tmp_path / "self").mkdir()

    status = bluez_support_status(run_command=_no_commands, proc_root=tmp_path)

    assert status == {
        "active": True,
        "packaged_drop_in": False,
        "exec_start": "/usr/libexec/bluetooth/bluetoothd " + flag.decode(),
    }


def test_openrc_explains_how_to_enable_experimental_mode(tmp_path, openrc):
    _process(tmp_path, 812, "bluetoothd", [b"/usr/libexec/bluetooth/bluetoothd"])

    status = bluez_support_status(run_command=_no_commands, proc_root=tmp_path)

    assert status["active"] is False
    assert status["activation_hint"] == OPENRC_BLUEZ_ACTIVATION_HINT
    assert "-E" in status["activation_hint"]
    assert "/etc/conf.d/bluetooth" in status["activation_hint"]
    assert "sudo rc-service bluetooth restart" in status["activation_hint"]


def test_missing_bluetoothd_or_unreadable_proc_is_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_manager, "init_system", lambda: service_manager.NO_SERVICE_MANAGER,
    )

    status = bluez_support_status(
        run_command=_no_commands, proc_root=tmp_path / "missing",
    )

    assert status["active"] is False
    assert status["exec_start"] == ""
    assert status["activation_hint"] == UNMANAGED_BLUEZ_ACTIVATION_HINT


def test_systemd_hosts_keep_querying_bluetooth_service(tmp_path):
    seen = []

    def run(argv, **_kwargs):
        seen.append(list(argv))
        return type("Result", (), {"returncode": 0, "stdout": "0\n"})()

    status = bluez_support_status(run_command=run, proc_root=tmp_path)

    assert "activation_hint" not in status
    assert seen == [
        ["/usr/bin/systemctl", "show", "bluetooth.service", "--property=ExecStart", "--value"],
        ["/usr/bin/systemctl", "show", "bluetooth.service", "--property=MainPID", "--value"],
    ]


def test_activation_without_systemd_reports_the_administrator_steps(tmp_path):
    commands = []

    with pytest.raises(PairingError) as failure:
        activate_bluez_support(
            status=lambda: {
                "active": False,
                "packaged_drop_in": False,
                "activation_hint": OPENRC_BLUEZ_ACTIVATION_HINT,
            },
            run_command=lambda *args, **kwargs: commands.append(args),
            systemctl_path=tmp_path / "missing-systemctl",
            sleep=lambda _seconds: None,
        )

    assert str(failure.value) == OPENRC_BLUEZ_ACTIVATION_HINT
    assert commands == []


def _compatibility_with_support(monkeypatch, version: str, support: dict) -> dict:
    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}

    def controller_info(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return type(
                "Result", (), {"returncode": 0, "stdout": f"{version}\n", "stderr": ""},
            )()
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "hci0: Primary controller\n"
                    "supported settings: powered ssp br/edr le advertising secure-conn\n"
                    "current settings: powered br/edr le advertising\n"
                ),
            },
        )()

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(pair_setup, "run_command", controller_info)
    monkeypatch.setattr(pair_setup, "bluez_support_status", lambda: support)
    return pair_setup.bluetooth_compatibility()


def test_manual_activation_is_explained_instead_of_offered(monkeypatch):
    status = _compatibility_with_support(monkeypatch, "5.86", {
        "active": False,
        "packaged_drop_in": False,
        "activation_hint": OPENRC_BLUEZ_ACTIVATION_HINT,
    })

    # No activation button that would always fail: pairing continues with
    # messages and contacts, and the issue text carries the manual steps.
    assert status["messages_supported"] is True
    assert status["notifications_supported"] is False
    assert status["issue"] == OPENRC_BLUEZ_ACTIVATION_HINT
    assert status["bluez_activation_hint"] == OPENRC_BLUEZ_ACTIVATION_HINT
    compatibility = BluetoothCompatibility.from_dict(status)
    assert compatibility.bluez_activation_hint == OPENRC_BLUEZ_ACTIVATION_HINT


def test_old_bluez_gets_no_experimental_mode_hint(monkeypatch):
    status = _compatibility_with_support(monkeypatch, "5.72", {
        "active": False,
        "packaged_drop_in": False,
        "activation_hint": OPENRC_BLUEZ_ACTIVATION_HINT,
    })

    assert "bluez_activation_hint" not in status
    assert status["issue"] != OPENRC_BLUEZ_ACTIVATION_HINT


def test_bluez_support_json_carries_the_activation_hint():
    support = BluezSupport.from_dict({
        "active": False, "packaged_drop_in": False, "exec_start": "",
        "activation_hint": OPENRC_BLUEZ_ACTIVATION_HINT,
    })

    assert support.to_dict()["activation_hint"] == OPENRC_BLUEZ_ACTIVATION_HINT
    # systemd results keep their previous JSON shape.
    assert "activation_hint" not in BluezSupport.from_dict(
        {"active": True, "packaged_drop_in": True, "exec_start": "x"},
    ).to_dict()


def test_openrc_ancs_repair_hints_name_rc_service(openrc, capsys):
    assert "sudo rc-service bluetooth restart" in ancs_unavailable_detail()
    assert "systemctl" not in ancs_unavailable_detail()

    _print_ancs_repair_hint()

    output = capsys.readouterr().out
    assert "Run: sudo rc-service bluetooth restart" in output
    assert "systemctl" not in output


def test_unknown_init_system_hints_avoid_systemctl(monkeypatch, capsys):
    monkeypatch.setattr(
        service_manager, "init_system", lambda: service_manager.NO_SERVICE_MANAGER,
    )

    assert "Try restarting the Bluetooth service" in ancs_unavailable_detail()
    _print_ancs_repair_hint()

    output = capsys.readouterr().out
    assert "Restart the Bluetooth service." in output
    assert "systemctl" not in output + ancs_unavailable_detail()
