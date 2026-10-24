"""Upgrade cleanup restores the desktop's audio roles without installing policy."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blueferry import wireplumber_policy as policy_module
from blueferry.wireplumber_policy import FRAGMENT_NAME, LEGACY_FRAGMENT_NAME, LegacyPhoneAudioPolicy


def test_cleanup_removes_both_owned_fragments_and_preserves_user_config(tmp_path, monkeypatch):
    path = tmp_path / FRAGMENT_NAME
    legacy = tmp_path / LEGACY_FRAGMENT_NAME
    other = tmp_path / "bluetooth-a2dp-autoconnect.conf"
    path.write_text("bluez5.roles = [ a2dp_source ]")
    legacy.write_text("override.bluez5.roles = [ a2dp_source ]")
    other.write_text("user settings")
    monkeypatch.setenv("BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE", "true")
    restarts = []
    policy = LegacyPhoneAudioPolicy(
        path=path, active=lambda: True, restart=lambda: restarts.append(True),
    )
    assert policy.remove() is True
    assert not path.exists() and not legacy.exists()
    assert other.read_text() == "user settings"
    assert restarts == [True]
    assert policy.remove() is False
    assert restarts == [True]


def test_no_owned_fragment_does_not_probe_or_restart_wireplumber(tmp_path):
    def forbidden():
        pytest.fail("nothing to clean up; do not query or restart services")

    policy = LegacyPhoneAudioPolicy(path=tmp_path / FRAGMENT_NAME, active=forbidden)
    assert policy.remove() is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dangling", [False, True])
def test_cleanup_unlinks_owned_symlinks_without_touching_targets(tmp_path, dangling):
    target = tmp_path / "user-config"
    if not dangling:
        target.write_text("keep me")
    path = tmp_path / FRAGMENT_NAME
    path.symlink_to(target)
    policy = LegacyPhoneAudioPolicy(
        path=path, active=lambda: False, manual_restart_hint=lambda: None,
    )
    assert policy.remove() is True
    assert not path.is_symlink()
    if not dangling:
        assert target.read_text() == "keep me"


def test_partial_cleanup_still_reloads_and_reports_the_failed_removal(tmp_path, monkeypatch, caplog):
    path = tmp_path / FRAGMENT_NAME
    legacy = tmp_path / LEGACY_FRAGMENT_NAME
    path.write_text("policy")
    legacy.write_text("old policy")
    unlink = Path.unlink

    def fail_one(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("denied")
        return unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)
    restarts = []
    policy = LegacyPhoneAudioPolicy(
        path=path, active=lambda: True, restart=lambda: restarts.append(True),
    )
    assert policy.remove() is True
    assert path.exists() and not legacy.exists()
    assert restarts == [True]
    assert "could not remove obsolete phone-audio policy" in caplog.text


def test_inactive_wireplumber_gets_a_manual_restart_hint(tmp_path, caplog):
    path = tmp_path / FRAGMENT_NAME
    path.write_text("policy")
    policy = LegacyPhoneAudioPolicy(
        path=path, active=lambda: False,
        restart=lambda: pytest.fail("inactive service should not start"),
        manual_restart_hint=lambda: "Restart WirePlumber manually",
    )
    assert policy.remove() is True
    assert "Restart WirePlumber manually" in caplog.text
    caplog.clear()
    assert policy.remove() is False
    assert not caplog.text


def test_restart_failure_does_not_reinstall_policy(tmp_path, caplog):
    path = tmp_path / FRAGMENT_NAME
    path.write_text("policy")

    def fail():
        raise OSError("restart failed")

    policy = LegacyPhoneAudioPolicy(path=path, active=lambda: True, restart=fail)
    assert policy.remove() is True
    assert not path.exists()
    assert "could not be restarted" in caplog.text


@pytest.mark.parametrize("wait", [False, True])
def test_cleanup_passes_restart_wait_to_service_manager(tmp_path, monkeypatch, wait):
    path = tmp_path / FRAGMENT_NAME
    path.write_text("policy")
    calls = []
    manager = SimpleNamespace(
        is_active=lambda service, **kw: service == "wireplumber",
        try_restart=lambda service, **kw: calls.append((service, kw)),
    )
    monkeypatch.setattr(policy_module, "user_service_manager", lambda _runner: manager)
    policy = LegacyPhoneAudioPolicy(path=path, wait_for_restart=wait)
    assert policy.remove() is True
    assert calls == [("wireplumber", {"wait": wait})]


def test_cleanup_respects_wireplumber_config_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WIREPLUMBER_CONFIG_DIR", f"{tmp_path}/first:{tmp_path}/second")
    assert policy_module.fragment_path() == tmp_path / "first/wireplumber.conf.d" / FRAGMENT_NAME
