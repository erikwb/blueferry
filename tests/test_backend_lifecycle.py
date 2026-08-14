"""Backend activation and package-upgrade lifecycle regressions."""
from __future__ import annotations

import json

import pytest

from blueferry import backend_lifecycle


class _Iface:
    def __init__(self, statuses):
        self.statuses = iter(statuses)

    def GetStatus(self, **_kwargs):
        return json.dumps(next(self.statuses))


class _Bus:
    def get_object(self, _name, _path):
        return object()


@pytest.fixture(autouse=True)
def _isolate_installed_build_and_systemctl(monkeypatch):
    monkeypatch.setattr(backend_lifecycle, "installed_build_sha", lambda: None)

    def unexpected_command(*_args, **_kwargs):
        raise AssertionError("test attempted to run a real lifecycle command")

    monkeypatch.setattr(backend_lifecycle, "run_command", unexpected_command)


def _dbus(monkeypatch, statuses):
    iface = _Iface(statuses)
    monkeypatch.setattr(backend_lifecycle, "get_session_bus", _Bus)
    monkeypatch.setattr(backend_lifecycle.dbus, "Interface", lambda *_args: iface)


def test_current_backend_is_not_restarted(monkeypatch):
    _dbus(monkeypatch, [{"backend_release": "0.6.0-6", "daemon": True}])
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "0.6.0-6")
    calls = []
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    status = backend_lifecycle.ensure_backend_current()

    assert status["daemon"] is True
    assert calls == []


def test_pre_lifecycle_backend_is_restarted_once(monkeypatch):
    _dbus(monkeypatch, [
        {"daemon": True},
        {"daemon": True},
        {"backend_release": "0.6.0-6", "daemon": True},
    ])
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "0.6.0-6")
    calls = []
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    backend_lifecycle.ensure_backend_current()

    assert calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "restart", "blueferry.service"],
    ]


def test_release_mismatch_is_restarted(monkeypatch):
    _dbus(monkeypatch, [
        {"backend_release": "0.6.0-5"},
        {"backend_release": "0.6.0-5"},
        {"backend_release": "0.6.0-6"},
    ])
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda *_args, **_kwargs: None,
    )

    status = backend_lifecycle.ensure_backend_current()

    assert status["backend_release"] == "0.6.0-6"


def test_same_release_with_a_different_build_sha_is_restarted(monkeypatch):
    old_build = "0.6.0-6+sha." + "a" * 12
    new_build = "0.6.0-6+sha." + "b" * 12
    _dbus(monkeypatch, [
        {"backend_release": "0.6.0-6", "_build_id": old_build},
        {"backend_release": "0.6.0-6", "_build_id": old_build},
        {"backend_release": "0.6.0-6", "_build_id": new_build},
    ])
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(
        backend_lifecycle,
        "installed_build_sha",
        lambda: "b" * 64,
    )
    calls = []
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    status = backend_lifecycle.ensure_backend_current()

    assert status["_build_id"] == new_build
    assert calls == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "restart", "blueferry.service"],
    ]


def test_missing_package_marker_does_not_restart(monkeypatch):
    _dbus(monkeypatch, [{"backend_release": "0.6.0-6", "daemon": True}])
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: None)
    calls = []
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    assert backend_lifecycle.ensure_backend_current()["daemon"] is True
    assert calls == []


def test_client_can_supply_a_worker_owned_status_reader(monkeypatch):
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(
        backend_lifecycle,
        "_status",
        lambda: (_ for _ in ()).throw(AssertionError("used process bus")),
    )

    status = backend_lifecycle.ensure_backend_current(
        status_reader=lambda: {"backend_release": "0.6.0-6", "daemon": True}
    )

    assert status["daemon"] is True
