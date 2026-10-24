"""Owner-only configuration cannot follow links or grow without bound."""
from __future__ import annotations

import json
import os
import stat

import pytest

from blueferry import config
from blueferry.private_files import atomic_write_private_text, private_file_lock
from blueferry.settings_store import SettingsStore


def test_private_lock_survives_atomic_replacement_and_exception(tmp_path):
    path = tmp_path / "local.env"
    with pytest.raises(RuntimeError, match="failed update"), private_file_lock(path):
        atomic_write_private_text(path, "first\n", maximum_bytes=1024)
        atomic_write_private_text(path, "second\n", maximum_bytes=1024)
        with pytest.raises(BlockingIOError), private_file_lock(path, blocking=False):
            pytest.fail("replacement lost the update lock")
        raise RuntimeError("failed update")

    with private_file_lock(path, blocking=False):
        assert path.read_text() == "second\n"
    lock_path = tmp_path / ".local.env.lock"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_private_lock_rejects_links_and_nonregular_files(tmp_path, kind):
    path = tmp_path / "local.env"
    lock_path = tmp_path / ".local.env.lock"
    victim = tmp_path / "untouched"
    victim.write_text("do not change\n")
    victim.chmod(0o644)
    if kind == "symlink":
        lock_path.symlink_to(victim)
    else:
        os.mkfifo(lock_path)

    with pytest.raises(OSError), private_file_lock(path):
        pytest.fail("unsafe lock accepted")

    assert victim.read_text() == "do not change\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_local_env_repairs_modes_and_ignores_unrelated_variables(tmp_path) -> None:
    directory = tmp_path / "blueferry"
    directory.mkdir(mode=0o755)
    path = directory / "local.env"
    path.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "BLUEFERRY_ANCS_APP_BLOCKLIST=com.example.Chat,com.example.Mail\n"
        "LD_PRELOAD=/tmp/hostile.so\n"
    )
    path.chmod(0o644)

    values = config.read_local_env(path)

    assert values == {
        "BLUEFERRY_MAC": "02:00:00:00:00:01",
        "BLUEFERRY_ANCS_APP_BLOCKLIST": (
            "com.example.Chat,com.example.Mail"
        ),
    }
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_local_env_does_not_follow_a_symlink(tmp_path) -> None:
    directory = tmp_path / "blueferry"
    directory.mkdir()
    target = tmp_path / "shared.env"
    target.write_text("BLUEFERRY_MAC=02:00:00:00:00:01\n")
    path = directory / "local.env"
    path.symlink_to(target)

    assert config.read_local_env(path) == {}


def test_oversized_local_env_is_ignored(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "blueferry"
    directory.mkdir()
    path = directory / "local.env"
    path.write_text("BLUEFERRY_MAC=" + "0" * 200)
    monkeypatch.setattr(config, "MAX_CONFIG_FILE_BYTES", 64)

    assert config.read_local_env(path) == {}


def test_settings_update_replaces_link_without_touching_target(tmp_path) -> None:
    directory = tmp_path / "blueferry"
    directory.mkdir()
    target = tmp_path / "victim.json"
    target.write_text('{"untouched": true}\n')
    path = directory / "settings.json"
    path.symlink_to(target)

    SettingsStore(path).update(desktop_notifications="none")

    assert json.loads(target.read_text()) == {"untouched": True}
    assert json.loads(path.read_text()) == {"desktop_notifications": "none"}
    assert not path.is_symlink()


def test_bluetooth_identifiers_from_the_environment_are_validated(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BLUEFERRY_MAC", "../../not-a-device")
    monkeypatch.setenv("BLUEFERRY_ADAPTER", "--index")

    assert config._configured_mac() == "AA:BB:CC:DD:EE:FF"
    assert config._configured_adapter() == "hci0"


def test_ancs_app_lists_accept_only_exact_bundle_ids(monkeypatch) -> None:
    monkeypatch.setenv(
        "BLUEFERRY_ANCS_APP_ALLOWLIST",
        " com.example.Allowed,invalid app,,com.example.Second ",
    )

    assert config._env_ancs_app_ids("BLUEFERRY_ANCS_APP_ALLOWLIST") == {
        "com.example.Allowed",
        "com.example.Second",
    }


def test_ancs_app_blocklist_wins_over_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "ANCS_APP_ALLOWLIST",
        frozenset({"com.example.Allowed", "com.example.Blocked"}),
    )
    monkeypatch.setattr(
        config,
        "ANCS_APP_BLOCKLIST",
        frozenset({"com.example.Blocked"}),
    )

    assert config.include_ancs_app("com.example.Allowed") is True
    assert config.include_ancs_app("com.example.Blocked") is False
    assert config.include_ancs_app("com.example.Unlisted") is False
    assert config.include_ancs_app("invalid app") is False


def test_explicitly_empty_ancs_allowlist_blocks_every_app(monkeypatch) -> None:
    monkeypatch.setenv("BLUEFERRY_ANCS_APP_ALLOWLIST", "")
    monkeypatch.setattr(config, "ANCS_APP_ALLOWLIST", frozenset())
    monkeypatch.setattr(config, "ANCS_APP_BLOCKLIST", frozenset())

    assert config._env_ancs_app_ids("BLUEFERRY_ANCS_APP_ALLOWLIST") == set()
    assert config.include_ancs_app("com.example.Anything") is False
