from __future__ import annotations

import dbus
import pytest

from blueferry import bus
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
        "close_obex_profile_bus",
        lambda _target: (_ for _ in ()).throw(RuntimeError("obexd gone")),
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
        "close_obex_profile_bus",
        lambda _target: (_ for _ in ()).throw(AssertionError("must not call obexd")),
    )

    manager.close_all(remove_remote=False)

    assert manager.map is None
    assert manager.pbap is None
    assert manager._closing is False


@pytest.fixture
def owned_sessions(monkeypatch):
    owners = []
    active = {}
    calls = []
    failures = []

    class Connection:
        closed = False

        def __init__(self, **kwargs):
            assert kwargs == {'private': True}
            self.owner = len(owners)
            owners.append(self)

        def get_object(self, *_args):
            return self

        def set_exit_on_disconnect(self, value):
            assert value is False

        def CreateSession(self, destination, options, **_kwargs):
            target = str(options['Target'])
            calls.append((self.owner, target))
            if failures:
                raise failures.pop(0)
            path = f'/session{self.owner}'
            active[path] = (self, target, destination)
            return path

        def close(self):
            self.closed = True
            for path, (owner, _, _) in list(active.items()):
                if owner is self:
                    del active[path]

    monkeypatch.setattr(bus.dbus, 'SessionBus', Connection)
    monkeypatch.setattr(sessions_mod.dbus, 'Interface', lambda obj, _iface: obj)
    yield owners, active, calls, failures
    bus.close_obex_worker_bus()


def test_retry_releases_own_stale_map_without_remove_session_or_pbap_loss(owned_sessions):
    owners, active, calls, _ = owned_sessions
    manager = SessionManager()
    manager.start_monitoring = lambda: None
    manager.open_all()
    old_map, pbap = manager.map, manager.pbap
    manager.map = None  # Local consumer discarded a stale MAP object.
    manager.open_all()

    assert calls == [(0, 'MAP'), (1, 'PBAP'), (2, 'MAP')]
    assert owners[0].closed
    assert not owners[1].closed
    assert old_map.path not in active
    assert pbap.path in active
    assert manager.pbap is pbap
    assert bus.get_obex_bus(manager.map.path + '/message1') is owners[2]


@pytest.mark.parametrize('name,message', [
    ('org.bluez.obex.Error.Failed', 'Forbidden'),
    ('org.freedesktop.DBus.Error.NoReply', 'Timed out'),
])
def test_failed_attempt_closes_its_owner_including_unknown_late_sessions(owned_sessions, name, message):
    owners, active, calls, failures = owned_sessions
    failures.append(dbus.exceptions.DBusException(message, name=name))
    if message == 'Forbidden':
        result = sessions_mod._create_session('MAP')
        assert result.path in active
        assert calls == [(0, 'MAP'), (1, 'MAP')]
    else:
        with pytest.raises(sessions_mod.SessionError, match='Timed out'):
            sessions_mod._create_session('MAP')
        assert not active
    assert owners[0].closed
