"""Controllers that support Bluetooth LE but run with it switched off (#192)."""

from __future__ import annotations

import pytest

from blueferry import bluetooth_capabilities as capabilities
from blueferry import pair_setup

_SUPPORTED_WITH_LE = (
    "powered connectable bondable ssp br/edr le advertising secure-conn"
)
_SUPPORTED_WITHOUT_LE = "powered connectable bondable ssp br/edr secure-conn"


class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_controller(monkeypatch, *, supported: str, current: str) -> None:
    class Manager:
        def GetManagedObjects(self):
            return {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}

    def run(command, **_kwargs):
        if command[0] == "bluetoothctl":
            return _Result("5.87\n")
        return _Result(
            "hci0:\tPrimary controller\n"
            "\taddr 02:00:00:00:00:02 version 7 manufacturer 15 class 0x6c010c\n"
            f"\tsupported settings: {supported}\n"
            f"\tcurrent settings: {current}\n"
        )

    monkeypatch.setattr(pair_setup, "_object_manager", lambda: Manager())
    monkeypatch.setattr(pair_setup, "run_command", run)
    monkeypatch.setattr(pair_setup, "bluez_support_status", lambda: {"active": True})


@pytest.mark.parametrize(
    ("supported", "current", "le_enabled", "le_disabled", "hardware_supported"),
    [
        pytest.param(
            _SUPPORTED_WITH_LE, "powered ssp br/edr le secure-conn",
            True, False, True, id="le-on",
        ),
        pytest.param(
            _SUPPORTED_WITH_LE, "powered ssp br/edr secure-conn",
            False, True, True, id="le-supported-but-off",
        ),
        pytest.param(
            _SUPPORTED_WITHOUT_LE, "powered ssp br/edr secure-conn",
            False, False, False, id="le-not-supported",
        ),
    ],
)
def test_compatibility_distinguishes_le_off_from_le_missing(
    monkeypatch, supported, current, le_enabled, le_disabled, hardware_supported,
):
    _fake_controller(monkeypatch, supported=supported, current=current)

    status = pair_setup.bluetooth_compatibility("hci0")

    assert status["le_enabled"] is le_enabled
    assert status["le_disabled"] is le_disabled
    assert status["hardware_supported"] is hardware_supported
    if le_disabled:
        # The hardware is fine; only the controller setting is wrong.
        assert status["pairing_ready"] is True
        assert "Bluetooth Low Energy is switched off" in status["issue"]
        assert "ControllerMode = dual" in status["issue"]
        assert "btmgmt --index 0 le on" in status["issue"]
        assert status["adapters"][0]["issue"] == status["issue"]
    else:
        assert "switched off" not in status["issue"]


def test_le_off_issue_names_a_bredr_controller_mode(monkeypatch, tmp_path):
    main_conf = tmp_path / "main.conf"
    main_conf.write_text("[General]\nControllerMode = bredr\n")
    monkeypatch.setattr(capabilities, "BLUEZ_MAIN_CONF", main_conf)
    _fake_controller(
        monkeypatch,
        supported=_SUPPORTED_WITH_LE,
        current="powered ssp br/edr secure-conn",
    )

    status = pair_setup.bluetooth_compatibility("hci0")

    assert status["controller_mode"] == "bredr"
    assert "ControllerMode = bredr" in status["issue"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[General]\nControllerMode = bredr\n", "bredr"),
        ("[General]\nControllerMode=Dual # comment\n", "dual"),
        ("[General]\n#ControllerMode = bredr\n", ""),
        ("[Policy]\nControllerMode = bredr\n", ""),
        ("[General]\nControllerMode = le\n", "le"),
        ("[General]\nControllerMode = something/else\n", "other"),
        ("", ""),
    ],
)
def test_bluez_controller_mode_reads_only_the_general_section(tmp_path, text, expected):
    path = tmp_path / "main.conf"
    path.write_text(text)

    assert capabilities.bluez_controller_mode(path) == expected


def test_bluez_controller_mode_is_empty_when_main_conf_is_missing(tmp_path):
    assert capabilities.bluez_controller_mode(tmp_path / "absent.conf") == ""


def test_setup_client_model_carries_the_le_state():
    from blueferry.setup_client import BluetoothCompatibility

    model = BluetoothCompatibility.from_dict(
        {"le_enabled": False, "le_disabled": True, "controller_mode": "bredr"}
    )

    assert model.le_disabled is True
    assert model.le_enabled is False
    assert model.to_dict()["controller_mode"] == "bredr"
    # Older payloads without the keys keep the previous behaviour.
    legacy = BluetoothCompatibility.from_dict({})
    assert legacy.le_enabled is True
    assert legacy.le_disabled is False
