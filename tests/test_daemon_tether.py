"""Daemon wiring for opt-in tethering, built with the real composition root."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import daemon as daemon_mod
from blueferry.errors import NotReadyError


class _Tether:
    def __init__(self, active: bool = False, alive: bool = False) -> None:
        self.active = active
        self.alive = alive
        self.calls: list[str] = []

    def link_alive(self) -> bool:
        return self.alive

    def probe_link(self) -> None:
        self.calls.append("probe")

    def reset_after_bluez_restart(self) -> None:
        self.calls.append("reset")

    def maybe_autoconnect(self) -> None:
        self.calls.append("maybe-autoconnect")

    def stop(self) -> None:
        self.calls.append("stop")


def test_tethering_is_off_and_not_automatic_by_default(make_daemon) -> None:
    instance = make_daemon()

    assert instance.tether.snapshot()["state"] == "off"
    assert instance.tether.snapshot()["autoconnect"] is False


def test_connect_needs_the_classic_link_the_bearer_supervisor_owns(make_daemon) -> None:
    instance = make_daemon()
    instance.bearers = SimpleNamespace(bredr_connected=False)

    # Raises before any backend choice, so no bus is touched.
    with pytest.raises(NotReadyError):
        instance.tether.connect()


def test_only_a_live_tether_link_holds_back_the_power_cycle(make_daemon) -> None:
    instance = make_daemon()
    instance.tether = _Tether(active=False, alive=False)
    assert instance._recovery_observation().busy is False

    # A merely "active" state (e.g. stuck connecting) must not block recovery.
    instance.tether = _Tether(active=True, alive=False)
    assert instance._recovery_observation().busy is False

    tether = _Tether(active=True, alive=True)
    instance.tether = tether
    assert instance._recovery_observation().busy is True
    # Each observation re-checks the link so stale state cannot persist.
    assert tether.calls == ["probe"]


def test_recovery_is_not_blocked_by_a_vanished_interface(make_daemon) -> None:
    instance = make_daemon()
    instance.tether._interface_exists = lambda _name: False
    instance.tether.observe_link(True, "bnep0")
    assert instance.tether.state == "connected"
    assert instance._recovery_observation().busy is False


def test_bluez_owner_loss_resets_tethering(make_daemon, monkeypatch) -> None:
    instance = make_daemon()
    tether = _Tether()
    instance.tether = tether
    instance._bluez_owner_match = object()
    instance.recovery = SimpleNamespace(invalidate=lambda **_k: None, active=False)
    monkeypatch.setattr(daemon_mod.bluez_setup, "forget_advert_registration", lambda: None)

    instance._on_bluez_owner_changed("org.bluez", ":1.1", "")

    assert tether.calls == ["reset"]


def test_bluez_restart_resets_tethering(make_daemon) -> None:
    instance = make_daemon()
    tether = _Tether()
    instance.tether = tether
    instance.adapter_class = SimpleNamespace(poke=lambda: None)
    instance.bearers = SimpleNamespace(
        hold_le=lambda: None, reset_after_bluez_restart=lambda: None,
    )
    instance.profiles = SimpleNamespace(reconnect=lambda *_a, **_k: None)
    instance._read_adapter_inhibitors = lambda: None

    instance._on_bluez_restart()

    assert tether.calls == ["reset"]


def test_ready_profiles_offer_autoconnect_only_after_map_pbap(make_daemon) -> None:
    instance = make_daemon()
    tether = _Tether()
    instance.tether = tether
    instance.contact_sync = SimpleNamespace(profiles_available=lambda: None)
    instance.solicitation = SimpleNamespace(set_needed=lambda _needed: None)

    instance._post_sessions_setup()

    assert tether.calls == ["maybe-autoconnect"]


def test_tether_changes_emit_only_the_content_free_signal(make_daemon) -> None:
    instance = make_daemon()
    emitted = []
    instance._dbus_service = SimpleNamespace(
        emit_tether_changed=lambda: emitted.append("tether"),
        emit_status=lambda: emitted.append("status"),
    )

    instance._emit_tether_changed()

    assert emitted == ["tether"]


def test_autoconnect_flag_reaches_the_controller(make_daemon, monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod.config, "TETHER_AUTOCONNECT", True)
    instance = make_daemon()
    assert instance.tether.snapshot()["autoconnect"] is True
