"""WirePlumber phone-audio policy is owned, reversible, and change-driven."""
from __future__ import annotations

from blueferry.wireplumber_policy import (
    FRAGMENT_TEXT,
    LEGACY_FRAGMENT_NAME,
    WirePlumberPhoneAudioPolicy,
    _parse_wireplumber_version,
)


def test_active_wireplumber_gets_phone_sink_roles_removed(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: True,
        restart=lambda: restarted.append(True),
    )

    assert policy.reconcile(enabled=True) is True

    text = path.read_text()
    assert text == FRAGMENT_TEXT
    assert "bluez5.roles" in text
    assert "override.bluez5.roles" not in text
    assert "a2dp_source" in text
    assert "a2dp_sink" not in text
    assert "hfp_hf" not in text
    assert "bluez5.auto-connect = [ ]" in text
    assert restarted == [True]


def test_matching_fragment_does_not_restart_wireplumber(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    path.parent.mkdir(parents=True)
    path.write_text(FRAGMENT_TEXT)
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: True,
        restart=lambda: restarted.append(True),
    )

    assert policy.reconcile(enabled=True) is False
    assert restarted == []


def test_legacy_fragment_is_replaced(tmp_path) -> None:
    directory = tmp_path / "wireplumber.conf.d"
    directory.mkdir(parents=True)
    path = directory / "99-blueferry-keep-phone-audio.conf"
    legacy = directory / LEGACY_FRAGMENT_NAME
    legacy.write_text("monitor.bluez.properties = { override.bluez5.roles = [ a2dp_source ] }\n")
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: True,
        restart=lambda: restarted.append(True),
    )

    assert policy.reconcile(enabled=True) is True
    assert path.read_text() == FRAGMENT_TEXT
    assert not legacy.exists()
    assert restarted == [True]


def test_disabling_removes_owned_fragments_only(tmp_path) -> None:
    directory = tmp_path / "wireplumber.conf.d"
    directory.mkdir(parents=True)
    path = directory / "99-blueferry-keep-phone-audio.conf"
    legacy = directory / LEGACY_FRAGMENT_NAME
    other = directory / "bluetooth-a2dp-autoconnect.conf"
    path.write_text(FRAGMENT_TEXT)
    legacy.write_text("legacy\n")
    other.write_text("user setting\n")
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: True,
        restart=lambda: restarted.append(True),
    )

    assert policy.reconcile(enabled=False) is True
    assert not path.exists()
    assert not legacy.exists()
    assert other.read_text() == "user setting\n"
    assert restarted == [True]


def test_unsupported_wireplumber_is_left_unconfigured(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: False,
        restart=lambda: restarted.append(True),
    )

    assert policy.reconcile(enabled=True) is False
    assert not path.exists()
    assert restarted == []


def test_parse_wireplumber_version_from_linked_banner() -> None:
    assert _parse_wireplumber_version(
        "wireplumber\nCompiled with libwireplumber 0.5.15\n"
    ) == (0, 5)
