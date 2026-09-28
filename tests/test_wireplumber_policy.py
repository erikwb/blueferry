"""WirePlumber phone-audio policy is owned, reversible, and change-driven."""
from __future__ import annotations

from types import SimpleNamespace

from blueferry.wireplumber_policy import (
    CALLS_FRAGMENT_TEXT,
    FRAGMENT_TEXT,
    LEGACY_FRAGMENT_NAME,
    WirePlumberPhoneAudioPolicy,
    _parse_wireplumber_version,
    _restart_wireplumber,
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
    assert "override.bluez5.roles" in text
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


def test_blocking_restart_waits_for_wireplumber(monkeypatch) -> None:
    seen: list[tuple[list[str], float]] = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("blueferry.wireplumber_policy.run_command", fake_run)

    _restart_wireplumber(wait=True)
    _restart_wireplumber()

    assert seen == [
        (
            ["/usr/bin/systemctl", "--user", "try-restart", "wireplumber.service"],
            30,
        ),
        (
            [
                "/usr/bin/systemctl",
                "--user",
                "--no-block",
                "try-restart",
                "wireplumber.service",
            ],
            5,
        ),
    ]


def test_pairing_policy_waits_for_wireplumber_reload(tmp_path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("blueferry.wireplumber_policy.run_command", fake_run)
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    policy = WirePlumberPhoneAudioPolicy(
        path=path,
        active=lambda: True,
        supported=lambda: True,
        wait_for_restart=True,
    )

    assert policy.reconcile(enabled=True) is True
    assert seen == [["/usr/bin/systemctl", "--user", "try-restart", "wireplumber.service"]]


def _roles(text: str) -> list[str]:
    line = next(line for line in text.splitlines() if "override.bluez5.roles" in line)
    return line.split("[", 1)[1].split("]", 1)[0].split()


def test_calls_keep_hands_free_roles_but_music_stays_on_the_phone(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    policy = WirePlumberPhoneAudioPolicy(
        path=path, active=lambda: False, supported=lambda: True, allow_calls=True,
    )

    assert policy.reconcile(enabled=True) is True

    text = path.read_text()
    assert text == CALLS_FRAGMENT_TEXT
    assert _roles(text) == ["a2dp_source", "hfp_ag", "bap_source", "hfp_hf", "hsp_hs"]
    assert "a2dp_sink" not in _roles(text)
    assert "bluez5.auto-connect = [ ]" in text
    assert _roles(FRAGMENT_TEXT) == ["a2dp_source", "hfp_ag", "bap_source"]


def test_toggling_calls_rewrites_the_owned_fragment_and_reloads(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    restarted = []

    def policy(allow_calls: bool) -> WirePlumberPhoneAudioPolicy:
        return WirePlumberPhoneAudioPolicy(
            path=path,
            active=lambda: True,
            supported=lambda: True,
            restart=lambda: restarted.append(allow_calls),
            allow_calls=allow_calls,
        )

    assert policy(False).reconcile(enabled=True) is True
    assert policy(True).reconcile(enabled=True) is True
    assert path.read_text() == CALLS_FRAGMENT_TEXT
    assert policy(True).reconcile(enabled=True) is False
    assert policy(False).reconcile(enabled=True) is True
    assert path.read_text() == FRAGMENT_TEXT
    assert restarted == [False, True, False]


def test_calls_without_keep_phone_audio_remove_the_fragment(tmp_path) -> None:
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    path.parent.mkdir(parents=True)
    path.write_text(CALLS_FRAGMENT_TEXT)
    policy = WirePlumberPhoneAudioPolicy(
        path=path, active=lambda: False, supported=lambda: True, allow_calls=True,
    )

    # KEEP_PHONE_AUDIO_ON_PHONE=false: BlueFerry manages no roles at all, so
    # the user's own WirePlumber configuration (e.g. hfp_hf) applies.
    assert policy.reconcile(enabled=False) is True
    assert not path.exists()


def test_policy_follows_the_calls_opt_in_by_default(tmp_path, monkeypatch) -> None:
    from blueferry import config

    path = tmp_path / "99-blueferry-keep-phone-audio.conf"
    monkeypatch.setattr(config, "CALLS_ENABLED", True)
    assert WirePlumberPhoneAudioPolicy(path=path).text == CALLS_FRAGMENT_TEXT
    monkeypatch.setattr(config, "CALLS_ENABLED", False)
    assert WirePlumberPhoneAudioPolicy(path=path).text == FRAGMENT_TEXT


def test_inactive_wireplumber_asks_the_user_to_restart_it(tmp_path, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="blueferry.wireplumber_policy")
    path = tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf"
    restarted = []
    policy = WirePlumberPhoneAudioPolicy(
        path=path, active=lambda: False, supported=lambda: True,
        restart=lambda: restarted.append(True), allow_calls=True,
    )

    assert policy.reconcile(enabled=True) is True
    assert restarted == []
    assert "restart WirePlumber to apply" in caplog.text
    caplog.clear()
    assert policy.reconcile(enabled=True) is False
    assert "restart WirePlumber" not in caplog.text
