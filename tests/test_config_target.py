"""Runtime target configuration honors environment precedence."""

import os

import pytest

from blueferry import config
from blueferry.phone_numbers import default_phone_region


@pytest.mark.parametrize("override, expected", [(None, "AU"), ("GB", "GB")])
def test_phone_region_local_env_honors_process_override(tmp_path, monkeypatch, override, expected):
    target = tmp_path / "local.env"
    target.write_text("BLUEFERRY_PHONE_REGION=AU\n", encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.delenv("BLUEFERRY_PHONE_REGION")
    if override is not None:
        monkeypatch.setenv("BLUEFERRY_PHONE_REGION", override)
    config._load_local_env()
    assert default_phone_region(os.environ) == expected


def test_current_target_reads_file_when_environment_was_not_explicit(
    tmp_path, monkeypatch
):
    target = tmp_path / "local.env"
    target.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\nBLUEFERRY_ADAPTER=hci7\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset())

    assert config.current_target() == ("02:00:00:00:00:01", "hci7")


def test_current_target_reports_cleared_file(tmp_path, monkeypatch):
    target = tmp_path / "local.env"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset())

    assert config.current_target() == ("", "hci0")


def test_current_target_keeps_explicit_environment_override(tmp_path, monkeypatch):
    target = tmp_path / "local.env"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.setattr(
        config,
        "EXPLICIT_ENV_KEYS",
        frozenset({"BLUEFERRY_MAC", "BLUEFERRY_ADAPTER"}),
    )
    monkeypatch.setenv("BLUEFERRY_MAC", "02:00:00:00:00:03")
    monkeypatch.setenv("BLUEFERRY_ADAPTER", "hci2")

    assert config.current_target() == ("02:00:00:00:00:03", "hci2")
