"""Owner-only configuration cannot follow links or grow without bound."""
from __future__ import annotations

import json
import stat

from blueferry import config
from blueferry.settings_store import SettingsStore


def test_local_env_repairs_modes_and_ignores_unrelated_variables(tmp_path) -> None:
    directory = tmp_path / "blueferry"
    directory.mkdir(mode=0o755)
    path = directory / "local.env"
    path.write_text(
        "BLUEFERRY_MAC=02:00:00:00:00:01\n"
        "LD_PRELOAD=/tmp/hostile.so\n"
    )
    path.chmod(0o644)

    values = config.read_local_env(path)

    assert values == {"BLUEFERRY_MAC": "02:00:00:00:00:01"}
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
