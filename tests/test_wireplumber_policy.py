"""WirePlumber phone-audio policy is owned, reversible, and change-driven."""
from __future__ import annotations

from types import SimpleNamespace

from blueferry.wireplumber_policy import (
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


def _openrc(monkeypatch, tmp_path):
    from blueferry import service_manager

    rc_service = tmp_path / "rc-service"
    rc_service.write_text("")
    rc_service.chmod(0o755)
    monkeypatch.setattr(service_manager, "RC_SERVICE_CANDIDATES", (str(rc_service),))
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    runtime = tmp_path / "runtime"
    (runtime / "openrc").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return str(rc_service)


def _started(seen):
    def fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=" * status: started\n", stderr="")

    return fake_run


def test_pairing_restarts_openrc_managed_wireplumber(tmp_path, monkeypatch) -> None:
    rc_service = _openrc(monkeypatch, tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr("blueferry.wireplumber_policy.run_command", _started(seen))
    policy = WirePlumberPhoneAudioPolicy(
        path=tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf",
        supported=lambda: True,
        wait_for_restart=True,
    )

    assert policy.reconcile(enabled=True) is True
    assert seen == [
        [rc_service, "--user", "wireplumber", "status"],
        [rc_service, "--user", "--ifstarted", "wireplumber", "restart"],
    ]


def test_daemon_does_not_block_on_openrc_wireplumber_restart(
    tmp_path, monkeypatch, caplog,
) -> None:
    rc_service = _openrc(monkeypatch, tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr("blueferry.wireplumber_policy.run_command", _started(seen))
    policy = WirePlumberPhoneAudioPolicy(
        path=tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf",
        supported=lambda: True,
    )

    with caplog.at_level("WARNING", logger="blueferry.wireplumber_policy"):
        assert policy.reconcile(enabled=True) is True

    assert seen == [[rc_service, "--user", "wireplumber", "status"]]
    assert "rc-service --user wireplumber restart" in caplog.text
    assert "Traceback" not in caplog.text


def test_session_launched_wireplumber_is_never_restarted(
    tmp_path, monkeypatch, caplog,
) -> None:
    from blueferry import service_manager

    # OpenRC without a user session: dbus-run-session desktops.
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        "blueferry.wireplumber_policy.run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran a command")),
    )
    policy = WirePlumberPhoneAudioPolicy(
        path=tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf",
        supported=lambda: True,
        wait_for_restart=True,
    )

    with caplog.at_level("WARNING", logger="blueferry.wireplumber_policy"):
        assert policy.reconcile(enabled=True) is True

    assert "restart WirePlumber" in caplog.text


def test_openrc_launcher_wireplumber_is_left_running_with_a_hint(
    tmp_path, monkeypatch, caplog,
) -> None:
    rc_service = _openrc(monkeypatch, tmp_path)
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=3, stdout=" * status: stopped\n", stderr="")

    monkeypatch.setattr("blueferry.wireplumber_policy.run_command", fake_run)
    policy = WirePlumberPhoneAudioPolicy(
        path=tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf",
        supported=lambda: True,
        manual_restart_hint=lambda: "restart WirePlumber yourself",
    )

    with caplog.at_level("WARNING", logger="blueferry.wireplumber_policy"):
        assert policy.reconcile(enabled=True) is True

    assert seen == [[rc_service, "--user", "wireplumber", "status"]]
    assert "restart WirePlumber yourself" in caplog.text


def test_default_hint_names_the_missing_openrc_service(tmp_path, monkeypatch) -> None:
    from blueferry.wireplumber_policy import _manual_restart_hint

    assert _manual_restart_hint() is None
    _openrc(monkeypatch, tmp_path)
    hint = _manual_restart_hint()
    assert hint is not None
    assert "OpenRC user service" in hint
    assert "restart WirePlumber" in hint


def test_inactive_systemd_wireplumber_stays_quiet(tmp_path, caplog) -> None:
    policy = WirePlumberPhoneAudioPolicy(
        path=tmp_path / "wireplumber.conf.d" / "99-blueferry-keep-phone-audio.conf",
        active=lambda: False,
        supported=lambda: True,
        restart=lambda: (_ for _ in ()).throw(AssertionError("restarted")),
    )

    with caplog.at_level("WARNING", logger="blueferry.wireplumber_policy"):
        assert policy.reconcile(enabled=True) is True

    assert caplog.text == ""
