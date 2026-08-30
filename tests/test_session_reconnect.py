from __future__ import annotations

import pytest

from blueferry.obex import sessions as sessions_mod
from blueferry.obex.sessions import ObexSession, SessionManager


def test_removed_map_session_is_invalidated_and_reported() -> None:
    reasons = []
    manager = SessionManager(on_lost=reasons.append)
    manager.map = ObexSession("MAP", "/org/bluez/obex/client/session1")
    manager.pbap = ObexSession("PBAP", "/org/bluez/obex/client/session2")

    manager._on_interfaces_removed(
        "/org/bluez/obex/client/session1", ["org.bluez.obex.Session1"]
    )

    assert manager.map is None
    assert manager.pbap is not None
    assert reasons == ["MAP session disappeared"]


def test_obexd_exit_invalidates_all_sessions_once() -> None:
    reasons = []
    manager = SessionManager(on_lost=reasons.append)
    manager.map = ObexSession("MAP", "/session1")
    manager.pbap = ObexSession("PBAP", "/session2")

    manager._on_name_owner_changed("org.bluez.obex", ":1.2", "")

    assert manager.map is None
    assert manager.pbap is None
    assert reasons == ["obexd exited"]


def test_unknown_object_send_error_triggers_reconnect() -> None:
    reasons = []
    manager = SessionManager(on_lost=reasons.append)
    manager.map = ObexSession("MAP", "/session1")

    manager.report_error(RuntimeError("org.freedesktop.DBus.Error.UnknownObject"))

    assert manager.map is None
    assert reasons


def test_failed_pbap_open_keeps_map_and_retries_only_pbap(monkeypatch) -> None:
    manager = SessionManager()
    attempts = []
    pbap_attempts = 0

    def create(target):
        nonlocal pbap_attempts
        attempts.append(target)
        if target == "MAP":
            return ObexSession("MAP", "/session/map")
        pbap_attempts += 1
        if pbap_attempts == 1:
            raise sessions_mod.SessionError("PBAP refused")
        return ObexSession("PBAP", "/session/pbap")

    monkeypatch.setattr(manager, "start_monitoring", lambda: None)
    monkeypatch.setattr(sessions_mod, "_remove_stale_sessions", lambda _x: None)
    monkeypatch.setattr(sessions_mod, "_create_session", create)

    with pytest.raises(sessions_mod.SessionError, match="PBAP refused"):
        manager.open_all()

    assert manager.map == ObexSession("MAP", "/session/map")
    assert manager.pbap is None

    manager.open_all()

    assert attempts == ["MAP", "PBAP", "PBAP"]
    assert manager.map == ObexSession("MAP", "/session/map")
    assert manager.pbap == ObexSession("PBAP", "/session/pbap")


def test_failed_map_open_still_opens_and_keeps_pbap(monkeypatch) -> None:
    manager = SessionManager()
    attempts = []

    def create(target):
        attempts.append(target)
        if target == "MAP":
            raise sessions_mod.SessionError("MAP transport disconnected")
        return ObexSession("PBAP", "/session/pbap")

    monkeypatch.setattr(manager, "start_monitoring", lambda: None)
    monkeypatch.setattr(sessions_mod, "_remove_stale_sessions", lambda _x: None)
    monkeypatch.setattr(sessions_mod, "_create_session", create)

    with pytest.raises(sessions_mod.SessionError, match="MAP transport"):
        manager.open_all()

    pbap = manager.pbap
    assert manager.map is None
    assert pbap == ObexSession("PBAP", "/session/pbap")

    with pytest.raises(sessions_mod.SessionError, match="MAP transport"):
        manager.open_all()

    assert attempts == ["MAP", "PBAP", "MAP"]
    assert manager.pbap is pbap


def test_close_clears_state_even_when_obexd_is_unreachable(monkeypatch) -> None:
    manager = SessionManager()
    manager.map = ObexSession("MAP", "/session/map")
    monkeypatch.setattr(
        sessions_mod,
        "_client",
        lambda: (_ for _ in ()).throw(RuntimeError("obexd gone")),
    )

    with pytest.raises(RuntimeError, match="obexd gone"):
        manager.close_all()

    assert manager.map is None
    assert manager.pbap is None
    assert manager._closing is False


def test_close_can_discard_lost_sessions_without_calling_obexd(monkeypatch) -> None:
    manager = SessionManager()
    manager.map = ObexSession("MAP", "/session/map")
    manager.pbap = ObexSession("PBAP", "/session/pbap")
    monkeypatch.setattr(
        sessions_mod,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not call obexd")),
    )

    manager.close_all(remove_remote=False)

    assert manager.map is None
    assert manager.pbap is None
    assert manager._closing is False
