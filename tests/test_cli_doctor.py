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
