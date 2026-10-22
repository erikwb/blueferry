"""Runtime target configuration honors environment precedence."""

import os
import stat
import uuid
from contextlib import contextmanager

import pytest

from blueferry import config, pair_setup
from blueferry.phone_numbers import default_phone_region
from blueferry.private_files import private_file_lock


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


def test_current_profile_id_migrates_old_pairing_once_and_remains_stable(
    tmp_path, monkeypatch
):
    target = tmp_path / "local.env"
    target.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\nBLUEFERRY_ADAPTER=hci7\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset())

    profile_id = config.current_profile_id("02:00:00:00:00:01")

    assert profile_id is not None
    assert uuid.UUID(profile_id).version == 4
    assert config.current_profile_id("02:00:00:00:00:01") == profile_id
    assert config.read_local_env(target)[config.PROFILE_ID_KEY] == profile_id
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_current_profile_id_is_omitted_for_a_different_environment_target(
    tmp_path, monkeypatch
):
    target = tmp_path / "local.env"
    target.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_PROFILE_ID=01234567-89ab-4cde-8fab-0123456789ab\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", target)
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset({"BLUEFERRY_MAC"}))
    monkeypatch.setenv("BLUEFERRY_MAC", "02:00:00:00:00:02")

    assert config.current_profile_id(config._configured_mac()) is None


@pytest.mark.parametrize("change", ["same_phone", "new_phone", "forget"])
def test_profile_migration_rereads_pairing_after_acquiring_lock(
    isolated_state, monkeypatch, change,
):
    target = config.LOCAL_ENV_PATH
    target.parent.mkdir()
    target.write_text("BLUEFERRY_MAC=02:00:00:00:00:01\n")
    monkeypatch.setattr(pair_setup, "LOCAL_ENV_PATH", target)
    saved = {}

    @contextmanager
    def pairing_finishes_before_lock(path, **kwargs):
        # The setup helper finishes between the optimistic read and the lock.
        if change == "forget":
            pair_setup.clear_local_target()
        else:
            mac = "02:00:00:00:00:02" if change == "new_phone" else "02:00:00:00:00:01"
            pair_setup.write_local_env(mac, "hci1", ancs_enabled=False)
        saved.update(config.read_local_env())
        with private_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(config, "private_file_lock", pairing_finishes_before_lock)

    profile_id = config.current_profile_id("02:00:00:00:00:01")

    assert config.read_local_env() == saved
    assert profile_id == (saved[config.PROFILE_ID_KEY] if change == "same_phone" else None)


def test_profile_migration_defers_while_locked_and_retries(isolated_state):
    target = config.LOCAL_ENV_PATH
    target.parent.mkdir()
    target.write_text("BLUEFERRY_MAC=02:00:00:00:00:01\n")

    with private_file_lock(target):
        assert config.current_profile_id("02:00:00:00:00:01") is None
        assert config.PROFILE_ID_KEY not in config.read_local_env()

    profile_id = config.current_profile_id("02:00:00:00:00:01")
    assert profile_id is not None
    # Reading an existing ID does not need to acquire the migration lock.
    with private_file_lock(target):
        assert config.current_profile_id("02:00:00:00:00:01") == profile_id
