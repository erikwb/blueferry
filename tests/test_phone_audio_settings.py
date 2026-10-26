from __future__ import annotations

import json

import pytest

from blueferry.phone_audio_settings import PhoneAudioSettings

MAC = "AA:BB:CC:DD:EE:FF"


def test_default_is_desktop_and_saved_choice_belongs_only_to_this_phone(tmp_path):
    path = tmp_path / "settings.json"
    settings = PhoneAudioSettings(MAC, path)
    assert settings.choice == "auto" and not path.exists()
    settings.set("phone")
    assert PhoneAudioSettings(MAC.lower(), path).choice == "phone"
    assert PhoneAudioSettings("AA:BB:CC:DD:EE:00", path).choice == "auto"
    settings.set("pc")
    assert PhoneAudioSettings(MAC, path).choice == "pc"
    settings.set("auto")
    assert PhoneAudioSettings(MAC, path).choice == "auto"


def test_preserves_other_preferences_and_rejects_invalid_choices(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"calls_enabled": true}')
    settings = PhoneAudioSettings(MAC, path)
    settings.set("pc")
    assert json.loads(path.read_text())["calls_enabled"] is True
    with pytest.raises(ValueError):
        settings.set("headphones")
    assert settings.choice == "pc"


@pytest.mark.parametrize("saved", [False, [], {"device": MAC, "route": []}, {"device": MAC, "route": "bogus"}])
def test_malformed_saved_preferences_are_inactive(tmp_path, saved):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"phone_audio_preference": saved}))
    assert PhoneAudioSettings(MAC, path).choice == "auto"


def test_cli_can_return_to_desktop_routing(monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from blueferry import cli_audio
    from blueferry.cli import app

    selected = []
    monkeypatch.setattr(cli_audio, "BackendClient", lambda: SimpleNamespace(
        set_phone_audio_route=selected.append,
    ))
    result = CliRunner().invoke(app, ["audio", "auto"])
    assert result.exit_code == 0
    assert selected == ["auto"]
    assert "follows the desktop again" in result.stdout
