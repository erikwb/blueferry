from __future__ import annotations

from blueferry.bluetooth_capabilities import (
    activate_bluez_support,
    ancs_limited_vendor,
    bluez_bearer_api_supported,
    bluez_stack,
    controller_hardware,
)
from blueferry.errors import PairingError


def _btmgmt(stdout: str):
    return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()


def test_realtek_and_broadcom_are_ancs_limited_vendors() -> None:
    assert ancs_limited_vendor("Realtek") is True
    assert ancs_limited_vendor("BROADCOM") is True
    assert ancs_limited_vendor("Cypress") is True
    assert ancs_limited_vendor("Intel") is False
    assert ancs_limited_vendor("MediaTek") is False
    assert ancs_limited_vendor("") is False


def test_bluez_bearer_api_requires_5_86_or_newer() -> None:
    assert bluez_bearer_api_supported("5.85") is False
    assert bluez_bearer_api_supported("") is False
    assert bluez_bearer_api_supported("unknown") is False
    assert bluez_bearer_api_supported("5.86") is True
    assert bluez_bearer_api_supported("5.87") is True


def test_controller_hardware_describes_a_realtek_usb_stick(tmp_path) -> None:
    usb = tmp_path / "bus" / "usb" / "1-4"
    iface = usb / "1-4:1.0"
    iface.mkdir(parents=True)
    usb.joinpath("idVendor").write_text("0bda\n")
    usb.joinpath("idProduct").write_text("c852\n")
    usb.joinpath("manufacturer").write_text("Realtek\n")
    usb.joinpath("product").write_text("RTL8852CE\n")
    drivers = tmp_path / "bus" / "usb" / "drivers" / "btusb"
    drivers.mkdir(parents=True)
    iface.joinpath("driver").symlink_to(drivers)
    hci = tmp_path / "class" / "bluetooth" / "hci0"
    hci.mkdir(parents=True)
    hci.joinpath("device").symlink_to(iface)

    identity = controller_hardware(
        "hci0",
        run_command=lambda *_args, **_kwargs: _btmgmt(
            "hci0: Primary controller\n"
            "\taddr 02:00:AA:BB:CC:DD version 11 manufacturer 93 class 0x7c0408\n"
        ),
        sys_root=tmp_path,
    )

    assert identity["vendor"] == "Realtek"
    assert identity["product"] == "RTL8852CE"
    assert identity["usb_id"] == "0bda:c852"
    assert identity["bus"] == "usb"
    assert identity["driver"] == "btusb"
    assert identity["manufacturer_id"] == 93
    assert identity["hci_version"] == 11
    assert "RTL8852CE" in str(identity["summary"])
    assert "0bda:c852" in str(identity["summary"])
    assert "02:00:AA:BB:CC:DD" not in str(identity)


def test_controller_hardware_describes_an_intel_pci_card(tmp_path) -> None:
    pci = tmp_path / "bus" / "pci" / "0000:00:14.3"
    pci.mkdir(parents=True)
    pci.joinpath("vendor").write_text("0x8086\n")
    pci.joinpath("device").write_text("0x2723\n")
    drivers = tmp_path / "bus" / "pci" / "drivers" / "iwlwifi"
    drivers.mkdir(parents=True)
    pci.joinpath("driver").symlink_to(drivers)
    hci = tmp_path / "class" / "bluetooth" / "hci1"
    hci.mkdir(parents=True)
    hci.joinpath("device").symlink_to(pci)

    identity = controller_hardware(
        "hci1",
        run_command=lambda *_args, **_kwargs: _btmgmt(
            "addr 11:22:33:44:55:66 version 10 manufacturer 2 class 0x7c0408\n"
        ),
        sys_root=tmp_path,
    )

    assert identity["vendor"] == "Intel"
    assert identity["pci_id"] == "8086:2723"
    assert identity["bus"] == "pci"
    assert identity["driver"] == "iwlwifi"
    assert "Intel" in str(identity["summary"])
    assert "11:22:33:44:55:66" not in str(identity)


def test_bluez_stack_reports_version_and_experimental_flag(tmp_path) -> None:
    commands = []
    daemon = tmp_path / "812"
    daemon.mkdir()
    daemon.joinpath("cmdline").write_bytes(
        b"/usr/lib/bluetooth/bluetoothd\0-E\0",
    )

    def run_command(argv, **_kwargs):
        commands.append(list(argv))
        if argv[0] == "bluetoothctl":
            return _btmgmt("bluetoothctl: 5.87\n")
        if "--property=MainPID" in argv:
            return _btmgmt("812\n")
        return _btmgmt(
            "{ path=/usr/lib/bluetooth/bluetoothd ; argv[]=/usr/lib/bluetooth/bluetoothd -E ; }\n"
        )

    stack = bluez_stack(run_command=run_command, proc_root=tmp_path)
    assert stack["bluez_version"] == "5.87"
    assert stack["experimental"] is True
    assert commands[0] == ["bluetoothctl", "--version"]


def test_bluez_stack_reuses_a_known_experimental_flag() -> None:
    commands = []

    def run_command(argv, **kwargs):
        commands.append((list(argv), kwargs.get("timeout")))
        return _btmgmt("bluetoothctl: 5.87\n")

    stack = bluez_stack(run_command=run_command, experimental=True, timeout=2)
    assert stack == {"experimental": True, "bluez_version": "5.87"}
    assert commands == [(["bluetoothctl", "--version"], 2)]


def test_controller_hardware_replaces_a_generic_usb_product(tmp_path) -> None:
    usb = tmp_path / "bus" / "usb" / "1-3"
    iface = usb / "1-3:1.0"
    iface.mkdir(parents=True)
    usb.joinpath("idVendor").write_text("0e8d\n")
    usb.joinpath("idProduct").write_text("7961\n")
    usb.joinpath("manufacturer").write_text("MediaTek\n")
    usb.joinpath("product").write_text("Wireless_Device\n")
    drivers = tmp_path / "bus" / "usb" / "drivers" / "btusb"
    drivers.mkdir(parents=True)
    iface.joinpath("driver").symlink_to(drivers)
    hci = tmp_path / "class" / "bluetooth" / "hci0"
    hci.mkdir(parents=True)
    hci.joinpath("device").symlink_to(iface)

    identity = controller_hardware(
        "hci0",
        run_command=lambda *_args, **_kwargs: _btmgmt(""),
        sys_root=tmp_path,
    )

    assert identity["vendor"] == "MediaTek"
    assert identity["product"] == "MT7921"
    assert identity["usb_id"] == "0e8d:7961"
    assert "Wireless_Device" not in str(identity["summary"])
    assert "MT7921" in str(identity["summary"])


def test_controller_hardware_ignores_invalid_adapter_names(tmp_path) -> None:
    identity = controller_hardware(
        "../etc",
        run_command=lambda *_args, **_kwargs: _btmgmt(""),
        sys_root=tmp_path,
    )
    assert identity == {"name": "../etc"}


def test_activate_bluez_support_delegates_authorization_to_systemctl(tmp_path) -> None:
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("")
    systemctl.chmod(0o755)
    statuses = iter(
        [
            {"active": False, "packaged_drop_in": True},
            {"active": True, "packaged_drop_in": True},
        ]
    )
    commands = []
    sleeps = []

    result = activate_bluez_support(
        status=lambda: next(statuses),
        run_command=lambda args, **kwargs: commands.append((args, kwargs)),
        systemctl_path=systemctl,
        sleep=sleeps.append,
    )

    assert result["active"] is True
    assert commands == [
        ([str(systemctl), "restart", "bluetooth.service"], {"timeout": 120})
    ]
    assert sleeps == [2]


def test_activate_bluez_support_requires_systemctl(tmp_path) -> None:
    try:
        activate_bluez_support(
            status=lambda: {"active": False, "packaged_drop_in": True},
            run_command=lambda *_args, **_kwargs: None,
            systemctl_path=tmp_path / "missing-systemctl",
        )
    except PairingError as error:
        assert str(error) == "systemctl is unavailable"
    else:
        raise AssertionError("activate_bluez_support accepted a missing systemctl")
