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
