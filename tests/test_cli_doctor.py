from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli, commands, config

_MNS_UUID = "00001133-0000-1000-8000-00805f9b34fb"


def _mns_probe(monkeypatch, *, running=True, powered=True, uuids=(_MNS_UUID,)):
    import dbus

    from blueferry import bus

    probes = []

    class SessionBus:
        def name_has_owner(self, name):
            probes.append(("owner", name))
            return running and name == "org.bluez.obex"

    class SystemBus:
        def get_name_owner(self, name):
            assert name == "org.bluez"
            return ":1.42"

        def get_object(self, name, path, *, introspect):
            assert name == ":1.42"  # Never activate bluetoothd during a probe.
            assert introspect is False
            probes.append(("adapter", path))
            return self

        def GetAll(self, interface, *, timeout):
            assert interface == "org.bluez.Adapter1"
            assert 0 < timeout <= 5
            return {"Powered": powered, "UUIDs": uuids}

    monkeypatch.setattr(bus, "get_session_bus", SessionBus)
    monkeypatch.setattr(bus, "get_system_bus", SystemBus)
    monkeypatch.setattr(dbus, "Interface", lambda proxy, _interface: proxy)
    return probes


def _healthy_non_cod_checks(monkeypatch) -> None:
    _mns_probe(monkeypatch)
    monkeypatch.setattr(cli, "_setup_logging", lambda _verbose: None)
    monkeypatch.setattr(config, "IPHONE_MAC", "02:00:00:00:00:01")
    monkeypatch.setattr(cli, "_find_obexd", lambda: "/usr/lib/bluetooth/obexd")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)

    def no_btmgmt(command, **_kwargs):
        from blueferry.errors import CommandError

        raise CommandError(tuple(command), "btmgmt is not available in tests")

    monkeypatch.setattr(commands, "run_command", no_btmgmt)


def test_doctor_warns_when_obex_runs_without_the_local_mns_service(monkeypatch, caplog):
    _matching_cod(monkeypatch)
    monkeypatch.setattr(config, "ADAPTER", "hci2")
    probes = _mns_probe(monkeypatch, uuids=["0000112e-0000-1000-8000-00805f9b34fb"])
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "MNS" in caplog.text and "missing on hci2" in caplog.text
    assert "Sending and contacts can still work" in caplog.text
    assert "-P irmc,pbap" in caplog.text
    assert ("adapter", "/org/bluez/hci2") in probes
    assert not any("/dev_" in str(probe) for probe in probes)


def test_doctor_accepts_the_local_mns_uuid_case_insensitively(monkeypatch, caplog):
    _matching_cod(monkeypatch)
    _mns_probe(monkeypatch, uuids=[_MNS_UUID.upper()])
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "All checks passed." in result.output
    assert "MNS" in caplog.text and "advertised" in caplog.text
    assert "-P irmc,pbap" not in caplog.text


def test_doctor_does_not_activate_obex_to_check_mns(monkeypatch, caplog):
    _matching_cod(monkeypatch)
    probes = _mns_probe(monkeypatch, running=False, uuids=[])
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "OBEX service not running" in caplog.text
    assert not any(kind == "adapter" for kind, _value in probes)
    assert "-P irmc,pbap" not in caplog.text


def test_doctor_rejects_an_invalid_adapter_before_the_mns_probe(monkeypatch, caplog):
    _matching_cod(monkeypatch)
    monkeypatch.setattr(config, "ADAPTER", "hci0/dev_02_00_00_00_00_01")
    probes = _mns_probe(monkeypatch)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "invalid Bluetooth adapter" in caplog.text
    assert not any(kind == "adapter" for kind, _value in probes)
    assert "-P irmc,pbap" not in caplog.text


def test_doctor_does_not_diagnose_missing_profiles_on_a_powered_off_adapter(
    monkeypatch, caplog,
):
    _matching_cod(monkeypatch)
    _mns_probe(monkeypatch, powered=False, uuids=[])

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "powered off" in caplog.text
    assert "-P irmc,pbap" not in caplog.text


def test_doctor_reports_an_unreadable_mns_probe_without_claiming_a_missing_service(
    monkeypatch, caplog,
):
    import dbus

    from blueferry import bus

    _matching_cod(monkeypatch)

    def unavailable():
        raise dbus.exceptions.DBusException("bus unavailable")

    monkeypatch.setattr(bus, "get_system_bus", unavailable)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "Could not check" in caplog.text and "MNS" in caplog.text
    assert "-P irmc,pbap" not in caplog.text


@pytest.mark.parametrize("uuids", [None, _MNS_UUID])
def test_doctor_does_not_treat_invalid_uuid_properties_as_missing_mns(
    monkeypatch, caplog, uuids,
):
    _matching_cod(monkeypatch)
    _mns_probe(monkeypatch, uuids=uuids)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "Could not check" in caplog.text and "MNS" in caplog.text
    assert "-P irmc,pbap" not in caplog.text


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


def _proc(tmp_path, processes):
    for pid, (comm, argv) in enumerate(processes, start=100):
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    return tmp_path


@pytest.mark.parametrize(("argv", "expected"), [
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E"], True),
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E", b"-P", b"hfp"], False),
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E", b"-Phfp"], False),
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E", b"--noplugin=sap,hfp"], False),
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E", b"--noplugin", b"h*"], False),
    ([b"/usr/libexec/bluetooth/bluetoothd", b"-E", b"-P", b"sap"], True),
    # Without -E bluetoothd does not register its experimental hfp profile.
    ([b"/usr/libexec/bluetooth/bluetoothd"], False),
])
def test_hfp_plugin_conflict_reads_the_bluetoothd_command_line(tmp_path, argv, expected):
    from blueferry import bluetooth_capabilities as caps

    root = _proc(tmp_path, [("ofonod", [b"/usr/sbin/ofonod"]), ("bluetoothd", argv)])
    assert caps.hfp_plugin_conflict(root) is expected


def test_hfp_plugin_check_is_skipped_without_ofono(tmp_path) -> None:
    from blueferry import bluetooth_capabilities as caps

    root = _proc(tmp_path, [("bluetoothd", [b"/usr/libexec/bluetooth/bluetoothd", b"-E"])])
    assert caps.hfp_plugin_conflict(root) is None


def _calls(monkeypatch, enabled: bool) -> None:
    from blueferry.calls import settings as calls_settings

    monkeypatch.setattr(
        calls_settings.CallsSettings, "enabled", property(lambda _self: enabled),
    )


def test_doctor_recommends_bluetooth_opts_and_reports_the_last_reset(
    monkeypatch, caplog,
) -> None:
    from blueferry import bluetooth_capabilities as caps

    _matching_cod(monkeypatch)
    _calls(monkeypatch, True)
    monkeypatch.setattr(caps, "hfp_plugin_conflict", lambda: True)
    monkeypatch.setattr(caps.service_manager, "init_system", lambda: "openrc")
    monkeypatch.setattr(cli, "_running_backend_status", lambda: {
        "profile_reset_auto": True,
        "profile_reset_suggested": "",
        "profile_reset_last": "hfp_powered_timeout",
        "profile_reset_last_at": 1_760_000_000,
        "profile_reset_last_auto": True,
    })
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "Checks completed with warnings." in result.output
    assert "/etc/conf.d/bluetooth" in caplog.text and "-P hfp" in caplog.text
    assert "Automatic profile reset: on" in caplog.text
    assert "Last profile reset" in caplog.text and "automatic" in caplog.text
    assert "HFP" in caplog.text


def test_doctor_skips_the_hfp_plugin_check_without_calls(monkeypatch, caplog) -> None:
    from blueferry import bluetooth_capabilities as caps

    _matching_cod(monkeypatch)
    _calls(monkeypatch, False)
    monkeypatch.setattr(caps, "hfp_plugin_conflict", lambda: True)
    monkeypatch.setattr(cli, "_running_backend_status", lambda: {
        "profile_reset_auto": True, "profile_reset_suggested": "hfp_powered_timeout",
    })
    caplog.set_level("INFO", logger="doctor")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 0
    assert "-P hfp" not in caplog.text
    assert "Run 'blueferry reconnect'" in caplog.text
    assert "No profile reset since the backend started" in caplog.text
