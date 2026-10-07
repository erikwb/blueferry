from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli, commands, config


def _healthy_non_cod_checks(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_setup_logging", lambda _verbose: None)
    monkeypatch.setattr(config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(cli, "_find_obexd", lambda: "/usr/lib/bluetooth/obexd")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)

    def no_btmgmt(command, **_kwargs):
        from blueferry.errors import CommandError

        raise CommandError(tuple(command), "btmgmt is not available in tests")

    monkeypatch.setattr(commands, "run_command", no_btmgmt)


def test_doctor_treats_an_unset_device_class_as_advisory(monkeypatch) -> None:
    _healthy_non_cod_checks(monkeypatch)
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: 0)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "FAILED" not in result.output


def test_doctor_still_fails_when_the_adapter_is_unreachable(monkeypatch) -> None:
    _healthy_non_cod_checks(monkeypatch)
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: None)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "One or more checks FAILED." in result.output


def _controller(monkeypatch, *, supported: str, current: str) -> None:
    from blueferry import bluetooth_capabilities

    class Result:
        returncode = 0
        stderr = ""
        stdout = (
            f"\tsupported settings: {supported}\n"
            f"\tcurrent settings: {current}\n"
        )

    monkeypatch.setattr(commands, "run_command", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: 0x6C010C)
    monkeypatch.setattr(cli.bluez_setup, "desired_cod_matches", lambda _cod: True)
    monkeypatch.setattr(bluetooth_capabilities, "bluez_controller_mode", lambda: "bredr")


def test_doctor_warns_when_bluetooth_le_is_switched_off(monkeypatch, caplog) -> None:
    _healthy_non_cod_checks(monkeypatch)
    _controller(
        monkeypatch,
        supported="powered ssp br/edr le advertising secure-conn",
        current="powered ssp br/edr secure-conn",
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "Bluetooth Low Energy is switched off" in caplog.text
    assert "ControllerMode = bredr" in caplog.text
    assert "ControllerMode = dual" in caplog.text


def test_doctor_accepts_bluetooth_le_that_is_on(monkeypatch, caplog) -> None:
    _healthy_non_cod_checks(monkeypatch)
    _controller(
        monkeypatch,
        supported="powered ssp br/edr le advertising secure-conn",
        current="powered ssp br/edr le secure-conn",
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "All checks passed." in result.output
    assert "switched off" not in caplog.text


def test_doctor_leaves_controllers_without_le_to_pairing(monkeypatch, caplog) -> None:
    _healthy_non_cod_checks(monkeypatch)
    _controller(
        monkeypatch,
        supported="powered ssp br/edr secure-conn",
        current="powered ssp br/edr secure-conn",
    )

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "switched off" not in caplog.text
