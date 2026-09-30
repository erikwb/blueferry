from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli, config


def _healthy_non_cod_checks(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_setup_logging", lambda _verbose: None)
    monkeypatch.setattr(config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(cli, "_find_obexd", lambda: "/usr/lib/bluetooth/obexd")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)


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
    monkeypatch.setattr(otp_clipboard.shutil, "which", lambda _tool: None)

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
