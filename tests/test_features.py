"""Feature switches in settings.json: allowlist, precedence, GetFeatures/SetFeature."""
from __future__ import annotations

import json
import os

import pytest

from blueferry import config, features
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.errors import InvalidArgumentsError, NotReadyError
from blueferry.features import FEATURES, FeatureSettings
from blueferry.settings_store import SettingsStore


def test_allowlist_is_a_subset_of_local_env_and_matches_the_table() -> None:
    assert {feature.variable for feature in FEATURES} == set(config.FEATURE_ENV_KEYS)
    assert config.FEATURE_ENV_KEYS <= config.LOCAL_ENV_KEYS
    assert set(features.running_values()) == set(features.BY_NAME)
    # Pairing target and adapter are never switchable from a client, and
    # switches with their own runtime setter are not duplicated here.
    assert not {
        "BLUEFERRY_MAC", "BLUEFERRY_ADAPTER", "BLUEFERRY_ANCS_ENABLED",
        "BLUEFERRY_ANCS_ACTIONS", "BLUEFERRY_CALLS_ENABLED", "BLUEFERRY_CALL_HISTORY_ENABLED",
        "BLUEFERRY_MISSED_CALL_NOTIFICATIONS", "BLUEFERRY_MEDIA_CONTROL_ENABLED",
        "BLUEFERRY_MEDIA_MPRIS_ENABLED", "BLUEFERRY_PROXIMITY_LOCK",
        "BLUEFERRY_PHONE_BATTERY_NOTIFY",
    } & set(config.FEATURE_ENV_KEYS)


def _settings(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_only_allowlisted_booleans_are_read(tmp_path) -> None:
    path = tmp_path / "settings.json"
    _settings(path, {"features": {
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT": False, "BLUEFERRY_MAC": True,
        "BLUEFERRY_MARK_READ_ON_DISMISS": "yes", "BLUEFERRY_CALLS_ENABLED": True,
        "BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE": False,  # retired switch
    }})
    assert config.read_feature_settings(path) == {
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT": False,
    }
    _settings(path, {"features": ["x"]})
    assert config.read_feature_settings(path) == {}
    assert config.read_feature_settings(tmp_path / "missing.json") == {}


def test_precedence_environment_then_settings_then_local_env(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "blueferry"
    _settings(directory / "settings.json", {"features": {
        "BLUEFERRY_SHOW_NOTIFICATION_CONTENT": True,
        "BLUEFERRY_MARK_READ_ON_DISMISS": False,
    }})
    local = directory / "local.env"
    local.write_text("BLUEFERRY_MARK_READ_ON_DISMISS=true\nBLUEFERRY_ANCS_ACTIONS=true\n")
    local.chmod(0o600)
    monkeypatch.setattr(config, "CONFIG_DIR", directory)
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", local)
    monkeypatch.setattr(
        config, "EXPLICIT_ENV_KEYS", frozenset({"BLUEFERRY_SHOW_NOTIFICATION_CONTENT"}),
    )
    for key in ("BLUEFERRY_MARK_READ_ON_DISMISS", "BLUEFERRY_ANCS_ACTIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "false")
    config._load_local_env()
    assert os.environ["BLUEFERRY_MARK_READ_ON_DISMISS"] == "false"      # settings over local.env
    assert os.environ["BLUEFERRY_ANCS_ACTIONS"] == "true"                # local.env alone
    assert os.environ["BLUEFERRY_SHOW_NOTIFICATION_CONTENT"] == "false"  # explicit env wins


def _running(**overrides):
    values = {feature.name: feature.default for feature in FEATURES}
    values.update(overrides)
    return lambda: values


def test_snapshot_and_set_report_source_and_restart(tmp_path) -> None:
    defaults = FeatureSettings(
        tmp_path / "other.json", running=_running(), explicit=frozenset(), local_env=dict,
    ).snapshot()
    assert defaults["mark_read_on_dismiss"] == {
        "value": True, "running": True, "source": "default",
        "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": False,
    }
    path = tmp_path / "config" / "settings.json"
    store = FeatureSettings(
        path, running=_running(mark_read_on_dismiss=True),
        explicit=frozenset({"BLUEFERRY_SHOW_NOTIFICATION_CONTENT"}),
        local_env=lambda: {"BLUEFERRY_MARK_READ_ON_DISMISS": "true"},
    )
    snapshot = store.snapshot()
    assert snapshot["mark_read_on_dismiss"]["source"] == "local.env"
    assert snapshot["show_notification_content"]["source"] == "environment"
    assert store.set("mark_read_on_dismiss", True) == "active"
    assert store.set("mark_read_on_dismiss", False) == "restart-required"
    assert store.set("show_notification_content", False) == "environment"
    snapshot = store.snapshot()
    assert snapshot["mark_read_on_dismiss"]["value"] is False
    assert snapshot["mark_read_on_dismiss"]["restart_required"] is True
    assert snapshot["mark_read_on_dismiss"]["source"] == "settings"
    assert snapshot["show_notification_content"]["value"] is True  # still the environment's
    assert SettingsStore(path).read()["features"]["BLUEFERRY_MARK_READ_ON_DISMISS"] is False
    with pytest.raises(KeyError):
        store.set("mac", True)


def test_backend_operations_validate(tmp_path) -> None:
    operations = BackendOperations(object())  # type: ignore[arg-type]
    with pytest.raises(NotReadyError):
        operations.get_features()
    operations = BackendOperations(object(), BackendDependencies(  # type: ignore[arg-type]
        features=FeatureSettings(tmp_path / "s.json", running=_running(),
                                 explicit=frozenset(), local_env=dict),
    ))
    with pytest.raises(InvalidArgumentsError):
        operations.set_feature("BLUEFERRY_MAC", True)
    with pytest.raises(InvalidArgumentsError):
        operations.set_feature("calls_enabled", True)  # has its own setter
    assert operations.set_feature("mark_read_on_dismiss", False) == "restart-required"
    assert operations.get_features()["mark_read_on_dismiss"]["value"] is False


@pytest.mark.parametrize("raw", ["false", "0", " NO ", "OFF", "true", "yes", "1", ""])
def test_snapshot_reads_current_local_env_after_it_changes(tmp_path, monkeypatch, raw) -> None:
    variable = "BLUEFERRY_MARK_READ_ON_DISMISS"
    monkeypatch.setenv(variable, raw)
    expected = config._env_bool(variable, True)
    store = FeatureSettings(
        tmp_path / "settings.json", running=_running(mark_read_on_dismiss=not expected),
        explicit=frozenset(), local_env=lambda: {variable: raw},
    )
    entry = store.snapshot()["mark_read_on_dismiss"]
    assert entry["value"] is expected
    assert entry["running"] is not expected
    assert entry["restart_required"] is True
