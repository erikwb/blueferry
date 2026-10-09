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


def _matching_cod(monkeypatch) -> None:
    _healthy_non_cod_checks(monkeypatch)
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: 0x240408)
    monkeypatch.setattr(cli.bluez_setup, "desired_cod_matches", lambda _cod: True)


def test_doctor_reports_a_stale_le_bond_with_the_remedy(monkeypatch, caplog) -> None:
    _matching_cod(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_running_backend_status",
        lambda: {
            "le_bond_suspect": True,
            "le_flap_count": 37,
            "last_le_disconnect_reason": "timeout",
        },
    )
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "dropped 37 times" in caplog.text
    assert "last reason: timeout" in caplog.text
    assert "Forget This Device" in caplog.text
    assert "bluetoothctl remove 02:00:00:00:00:01" in caplog.text


def test_doctor_reports_a_healthy_le_link_and_sanitizes_status(monkeypatch, caplog) -> None:
    _matching_cod(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_running_backend_status",
        lambda: {
            "le_bond_suspect": "yes",
            "le_flap_count": 2,
            "last_le_disconnect_reason": "/org/bluez/hci0/dev_02_00",
        },
    )
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "All checks passed." in result.output
    assert "no stale-bond pattern (2 recent short drops" in caplog.text
    assert "dev_02" not in caplog.text


def test_doctor_does_not_start_the_backend(monkeypatch, caplog) -> None:
    _matching_cod(monkeypatch)

    class Bus:
        @staticmethod
        def name_has_owner(_name):
            return False

    import blueferry.bus

    monkeypatch.setattr(blueferry.bus, "get_session_bus", lambda: Bus())
    monkeypatch.setattr(
        cli,
        "_backend_client",
        lambda: (_ for _ in ()).throw(AssertionError("backend activated")),
    )
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Backend not running" in caplog.text


def _forbid_processes(monkeypatch) -> list:
    import subprocess

    from blueferry import commands

    started = []

    def forbidden(*args, **_kwargs):
        started.append(args)
        raise AssertionError("doctor must not start a real process in tests")

    monkeypatch.setattr(commands.subprocess, "run", forbidden)
    monkeypatch.setattr(commands.subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    return started


def test_doctor_warns_when_autocopy_has_no_clipboard_helper(monkeypatch) -> None:
    from blueferry import otp_clipboard

    _healthy_non_cod_checks(monkeypatch)
    started = _forbid_processes(monkeypatch)
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: 0x240404)
    monkeypatch.setattr(cli.bluez_setup, "desired_cod_matches", lambda _cod: True)
    monkeypatch.setattr(config, "OTP_AUTOCOPY", True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(otp_clipboard.shutil, "which", lambda _tool, path=None: None)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert started == []


def test_doctor_with_a_real_path_probes_only_through_the_fake_runner(monkeypatch) -> None:
    import subprocess

    from blueferry import commands

    _healthy_non_cod_checks(monkeypatch)
    started = _forbid_processes(monkeypatch)
    probes = []

    def fake_run(argv, **_kwargs):
        if not argv[0].endswith("/wl-copy"):
            from blueferry.errors import CommandError

            raise CommandError(tuple(argv), "unrelated doctor probe unavailable in tests")
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "-o, --paste-once", "")

    monkeypatch.setattr(commands, "run_command", fake_run)
    monkeypatch.setattr(cli.bluez_setup, "current_cod", lambda: 0x240404)
    monkeypatch.setattr(cli.bluez_setup, "desired_cod_matches", lambda _cod: True)
    monkeypatch.setattr(config, "OTP_AUTOCOPY", True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    result = CliRunner().invoke(cli.app, ["doctor"])

    # With or without wl-copy on this machine's PATH the check warns: either
    # no helper, or a helper whose (faked) help lacks --sensitive.
    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert started == []
    assert all(argv[1:] == ["--help"] and argv[0].endswith("/wl-copy") for argv in probes)
    import shutil

    if shutil.which("wl-copy"):
        assert probes, "the capability probe must go through commands.run_command"
