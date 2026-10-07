"""Controllers that support Bluetooth LE but run with it switched off (#192)."""

from __future__ import annotations

import pytest

from blueferry import bluetooth_capabilities as capabilities
from blueferry import config, pair_setup

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


def _le_off_compatibility(monkeypatch, *, bearer_api_active: bool = True) -> list:
    """Run _prepare_pairing against fake capabilities with LE switched off."""
    monkeypatch.setattr(config, "STATE_DIR", config.STATE_DIR / "le-disabled-tests")
    device = pair_setup.PairedDevice(
        mac="02:00:00:00:00:01",
        name="Test iPhone",
        icon="phone",
        trusted=True,
        connected=False,
        paired=True,
        adapter_path="/org/bluez/hci0",
        device_path="/org/bluez/hci0/dev_02_00_00_00_00_01",
        uuids=frozenset(),
        services_resolved=False,
    )
    monkeypatch.setattr(pair_setup, "_device", lambda _mac, **_kwargs: device)
    monkeypatch.setattr(
        pair_setup,
        "bluetooth_compatibility",
        lambda _adapter: {
            "pairing_ready": True,
            "hardware_supported": True,
            "notifications_supported": True,
            "bearer_api_active": bearer_api_active,
            "low_energy": True,
            "le_enabled": False,
            "le_disabled": True,
            "controller_mode": "bredr",
            "advertising": True,
            "issue": capabilities.le_disabled_issue("hci0", "bredr"),
        },
    )
    monkeypatch.setattr(pair_setup, "_controller_snapshot", lambda _adapter, value: dict(value))
    monkeypatch.setattr(pair_setup, "_bluetooth_session_owners", lambda: [])
    monkeypatch.setattr(pair_setup, "_take_pending_teardown", lambda _adapter: None)
    monkeypatch.setattr(pair_setup, "_snapshot_phone", lambda *_args: None)
    monkeypatch.setattr(pair_setup, "_record_bluez_state", lambda *_args, **_kwargs: None)
    return [device]


def test_full_mode_pairing_stops_before_the_advertisement_when_le_is_off(monkeypatch):
    from blueferry import bluez_setup

    (device,) = _le_off_compatibility(monkeypatch)
    monkeypatch.setattr(
        bluez_setup,
        "register_advert",
        lambda *_args, **_kwargs: pytest.fail("advertisement must not be attempted"),
    )
    monkeypatch.setattr(
        pair_setup,
        "_run_pairing_transaction",
        lambda *_args, **_kwargs: pytest.fail("pairing must not start"),
    )

    with pytest.raises(pair_setup.PairingError) as caught:
        pair_setup.complete_pairing(device.mac, _allow_headless=True)

    error = caught.value
    assert error.reason == "le_disabled"
    assert "Bluetooth Low Energy is switched off" in str(error)
    assert "ControllerMode = dual" in str(error)
    report = pair_setup.json.loads(
        pair_setup.Path(error.report_path).read_text(encoding="utf-8")
    )
    assert report["outcome"]["reason"] == "le_disabled"
    assert report["controller"]["le_disabled"] is True
    assert report["controller"]["controller_mode"] == "bredr"
    events = [entry["event"] for entry in report["timeline"]]
    assert "le_disabled" in events
    assert "advert_register_sent" not in events
    from blueferry import quirks_report

    assert quirks_report.issue_title(report).endswith(
        "Bluetooth LE is switched off on the adapter"
    )


def test_compatibility_mode_pairing_continues_without_solicitation_when_le_is_off(
    monkeypatch, caplog,
):
    (device,) = _le_off_compatibility(monkeypatch)
    attempt = pair_setup.quirks_report.start_attempt(interactive=False)

    preparation = pair_setup._prepare_pairing(
        device.mac,
        adapter=None,
        compatibility_mode=True,
        explicit_pairing=False,
        interactive=False,
        attempt=attempt,
    )

    assert preparation.policy.ancs_enabled is False
    assert preparation.policy.solicitation_enabled is False
    assert "continuing with MAP/PBAP only" in caplog.text
    assert "le_disabled" in [entry["event"] for entry in attempt["timeline"]]


def test_solicitation_stays_enabled_when_le_is_on():
    policy = pair_setup.resolve_pairing_policy(
        {
            "notifications_supported": False,
            "low_energy": True,
            "le_enabled": True,
            "advertising": True,
        },
        force_compatibility=True,
    )

    assert policy.solicitation_enabled is True


def test_pairing_outcome_omits_reason_for_unclassified_errors():
    from blueferry import pairing_diagnostics

    attempt = pair_setup.quirks_report.start_attempt(interactive=False)

    outcome = pairing_diagnostics.pairing_outcome(
        attempt, None, pair_setup.PairingError("The ANCS advertisement did not activate"),
    )

    assert "reason" not in outcome


def _fake_systemctl(monkeypatch, *, returncode: int = 0, stderr: str = "") -> list:
    from blueferry import bluez_setup

    calls: list = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        result = _Result("", returncode)
        result.stderr = stderr
        return result

    monkeypatch.setattr(bluez_setup, "run_command", run)
    return calls


def test_enable_le_starts_only_the_packaged_unit(monkeypatch):
    from blueferry import bluez_setup

    calls = _fake_systemctl(monkeypatch)

    bluez_setup.enable_le("hci7")

    assert calls[0][0] == [
        "/usr/bin/systemctl", "start", "blueferry-btmgmt-le-on@7.service",
    ]
    assert calls[0][1]["timeout"] == 120


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Interactive authentication required.", "sudo btmgmt --index 7 le on"),
        (
            "Unit blueferry-btmgmt-le-on@7.service not found.",
            "Bluetooth LE service is not installed",
        ),
        ("Failed to start unit.", "ControllerMode = dual"),
    ],
)
def test_enable_le_failures_point_at_a_manual_fix(monkeypatch, stderr, expected):
    from blueferry import bluez_setup

    _fake_systemctl(monkeypatch, returncode=1, stderr=stderr)

    with pytest.raises(pair_setup.PairingError, match=expected.replace(".", r"\.")):
        bluez_setup.enable_le("hci7")


def test_enable_le_rejects_an_invalid_adapter_without_running_anything(monkeypatch):
    from blueferry import bluez_setup

    calls = _fake_systemctl(monkeypatch)

    with pytest.raises(pair_setup.PairingError):
        bluez_setup.enable_le("hci0; reboot")
    assert calls == []


@pytest.mark.parametrize("le_after", [True, False])
def test_enable_controller_le_verifies_current_settings(monkeypatch, le_after):
    from blueferry import bluez_setup

    enabled: list[str] = []
    monkeypatch.setattr(bluez_setup, "enable_le", enabled.append)
    current = "powered ssp br/edr secure-conn" + (" le" if le_after else "")
    _fake_controller(monkeypatch, supported=_SUPPORTED_WITH_LE, current=current)

    if le_after:
        status = pair_setup.enable_controller_le("hci0")
        assert status["le_enabled"] is True
        assert status["le_disabled"] is False
    else:
        with pytest.raises(pair_setup.PairingError, match="still switched off") as caught:
            pair_setup.enable_controller_le("hci0")
        assert caught.value.reason == "le_disabled"
    assert enabled == ["hci0"]


def _le_off_model(**overrides):
    from blueferry.setup_client import BluetoothCompatibility

    value = {
        "adapter": "hci0",
        "available": True,
        "low_energy": True,
        "le_enabled": False,
        "le_disabled": True,
        "notifications_supported": True,
        "pairing_ready": True,
        "issue": capabilities.le_disabled_issue("hci0"),
    }
    value.update(overrides)
    return BluetoothCompatibility.from_dict(value)


class _FakeSetup:
    def __init__(self, *, result=None, error=None) -> None:
        self.enabled: list[str] = []
        self._result = result
        self._error = error

    def enable_le(self, adapter):
        self.enabled.append(adapter)
        if self._error is not None:
            raise self._error
        return self._result


def _answers(monkeypatch, *answers: bool) -> list[str]:
    from blueferry import pairing_cli

    prompts: list[str] = []
    replies = iter(answers)
    monkeypatch.setattr(
        pairing_cli.typer,
        "confirm",
        lambda prompt, **_kwargs: prompts.append(prompt) or next(replies),
    )
    return prompts


def test_cli_switches_on_le_only_after_explicit_confirmation(monkeypatch, capsys):
    from blueferry import pairing_cli

    prompts = _answers(monkeypatch, True)
    enabled = _le_off_model(le_enabled=True, le_disabled=False, issue="")
    setup = _FakeSetup(result=enabled)

    resolved = pairing_cli._resolve_disabled_le(
        setup, _le_off_model(), compatibility_mode=False,
    )

    assert resolved == (enabled, False)
    assert setup.enabled == ["hci0"]
    assert prompts[0].startswith("Switch on Bluetooth LE on hci0 now?")
    output = capsys.readouterr().out
    assert "Bluetooth Low Energy is switched off" in output
    assert "lasts until bluetoothd restarts" in output
    assert "BlueFerry does not edit that file" in output


def test_cli_declining_le_offers_compatibility_mode(monkeypatch):
    from blueferry import pairing_cli

    prompts = _answers(monkeypatch, False, True)
    setup = _FakeSetup()
    compatibility = _le_off_model()

    resolved = pairing_cli._resolve_disabled_le(
        setup, compatibility, compatibility_mode=False,
    )

    assert resolved == (compatibility, True)
    assert setup.enabled == []
    assert "compatibility mode" in prompts[1]


def test_cli_failed_le_switch_falls_back_or_stops(monkeypatch, capsys):
    from blueferry import pairing_cli

    _answers(monkeypatch, True, False)
    setup = _FakeSetup(error=pair_setup.PairingError("Bluetooth LE is still switched off"))

    resolved = pairing_cli._resolve_disabled_le(
        setup, _le_off_model(), compatibility_mode=False,
    )

    assert resolved is None
    assert "still switched off" in capsys.readouterr().out


def test_cli_compatibility_mode_keeps_messaging_without_prompting(monkeypatch):
    from blueferry import pairing_cli

    prompts = _answers(monkeypatch)
    setup = _FakeSetup()
    compatibility = _le_off_model()

    resolved = pairing_cli._resolve_disabled_le(
        setup, compatibility, compatibility_mode=True,
    )

    assert resolved == (compatibility, True)
    assert prompts == []
    assert setup.enabled == []


def test_cli_le_on_needs_no_prompt(monkeypatch):
    from blueferry import pairing_cli

    prompts = _answers(monkeypatch)
    compatibility = _le_off_model(le_enabled=True, le_disabled=False, issue="")

    assert pairing_cli._resolve_disabled_le(
        _FakeSetup(), compatibility, compatibility_mode=False,
    ) == (compatibility, False)
    assert prompts == []
