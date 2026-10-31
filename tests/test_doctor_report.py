"""In-app diagnostics use current configuration without accessing Bluetooth."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("explicit", [False, True])
def test_doctor_reads_saved_settings_and_honors_explicit_overrides(tmp_path, monkeypatch, explicit):
    from blueferry import config, doctor_report

    root = tmp_path / "blueferry"
    root.mkdir(mode=0o700)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"features": {"BLUEFERRY_SHOW_NOTIFICATION_CONTENT": False}}))
    settings.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # The GUI loaded this old file-backed value before the user saved "off".
    monkeypatch.setenv("BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "true")
    monkeypatch.setattr(config, "EXPLICIT_ENV_KEYS", frozenset(
        {"BLUEFERRY_SHOW_NOTIFICATION_CONTENT"} if explicit else {}
    ))
    command = [sys.executable, "-c", "from blueferry import config; print(config.SHOW_NOTIFICATION_CONTENT)"]
    fresh_environment = dict(os.environ)
    fresh_environment.pop("BLUEFERRY_SHOW_NOTIFICATION_CONTENT")
    run = subprocess.run
    fresh = run(command, env=fresh_environment, capture_output=True, text=True, check=True).stdout.strip()
    assert fresh == "False"
    # Exercise the actual doctor launch environment with a configuration-only
    # child, avoiding any real Bluetooth or keyring access.
    monkeypatch.setattr(doctor_report.subprocess, "run", lambda _argv, **kwargs: run(command, **kwargs))
    assert doctor_report.run_doctor().text == ("True" if explicit else fresh)
