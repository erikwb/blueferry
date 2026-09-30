"""Runtime target configuration honors environment precedence."""

from blueferry import config


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


def test_tether_settings_are_opt_in_and_validated(monkeypatch) -> None:
    assert {"BLUEFERRY_TETHER_AUTOCONNECT", "BLUEFERRY_TETHER_BACKEND"} <= config.LOCAL_ENV_KEYS
    monkeypatch.delenv("BLUEFERRY_TETHER_AUTOCONNECT", raising=False)
    assert config._env_bool("BLUEFERRY_TETHER_AUTOCONNECT", False) is False

    for raw, expected in (
        ("", "auto"), ("wicd", "auto"), ("NetworkManager", "networkmanager"),
        (" bluez ", "bluez"),
    ):
        monkeypatch.setenv("BLUEFERRY_TETHER_BACKEND", raw)
        assert config._tether_backend() == expected
