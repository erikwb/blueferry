"""`blueferry proximity-lock` CLI with a fake backend; never locks anything."""
from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli_proximity
from blueferry.cli import app
from blueferry.client import BackendError
from blueferry.models import BackendStatus


class _Backend:
    def __init__(self, status: dict | None) -> None:
        self._status = status
        self.set_calls: list[tuple[bool, int]] = []

    def status(self) -> BackendStatus:
        if self._status is None:
            raise BackendError("not running")
        return BackendStatus.from_dict(self._status)

    def set_proximity_lock(self, enabled: bool, grace: int) -> dict:
        self.set_calls.append((enabled, grace))
        return {
            "proximity_lock": "idle" if enabled else "disabled",
            "proximity_lock_enabled": enabled,
            "proximity_lock_grace_sec": grace,
        }


def _run(monkeypatch, backend, *args, probe=None):
    monkeypatch.setattr(cli_proximity, "_client", lambda: backend)
    probed: list[tuple[str, str]] = []

    def fake_probe(bus: str, name: str):
        probed.append((bus, name))
        return probe

    monkeypatch.setattr(cli_proximity, "_bus_name_available", fake_probe)
    result = CliRunner().invoke(app, ["proximity-lock", *args])
    return result, probed


_GRACE = {
    "proximity_lock": "grace",
    "proximity_lock_enabled": True,
    "proximity_lock_grace_sec": 60,
    "proximity_lock_remaining_sec": 12,
    "proximity_lock_inhibited": "",
    "proximity_lock_last_result": "",
}


def test_status_is_the_default_subcommand(monkeypatch) -> None:
    result, _ = _run(monkeypatch, _Backend(_GRACE))
    assert result.exit_code == 0
    assert "grace period running" in result.output
    assert "Locking in: 12 s" in result.output


def test_status_reports_inhibitors_and_failures(monkeypatch) -> None:
    status = dict(
        _GRACE,
        proximity_lock="idle",
        proximity_lock_inhibited="adapter-off,sleep",
        proximity_lock_last_result="failed",
    )
    result, _ = _run(monkeypatch, _Backend(status), "status")
    assert "desktop Bluetooth is off; system suspend" in result.output
    assert "failed" in result.output


def test_status_without_daemon_fails_cleanly(monkeypatch) -> None:
    result, _ = _run(monkeypatch, _Backend(None), "status")
    assert result.exit_code == 2


def test_old_daemon_without_the_feature_is_explained(monkeypatch) -> None:
    result, _ = _run(monkeypatch, _Backend({"daemon": True}), "status")
    assert "does not support proximity lock" in result.output


def test_dry_run_explains_and_never_sets_or_locks(monkeypatch) -> None:
    backend = _Backend(dict(_GRACE, proximity_lock="disabled", proximity_lock_enabled=False))
    result, probed = _run(monkeypatch, backend, "test", probe=True)

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "never unlocks" in result.output
    assert "Proximity lock is off" in result.output
    assert "org.freedesktop.ScreenSaver.Lock()" in result.output
    assert "GetSessionByPID" in result.output
    assert backend.set_calls == []
    # Only read-only name-owner checks, never a Lock call.
    assert probed == [
        ("session", "org.freedesktop.ScreenSaver"),
        ("system", "org.freedesktop.login1"),
    ]


def test_dry_run_works_without_daemon(monkeypatch) -> None:
    result, _ = _run(monkeypatch, _Backend(None), "test", probe=None)
    assert result.exit_code == 0
    assert "not running" in result.output
    assert "could not check" in result.output


def test_enable_keeps_current_grace_and_disable_turns_off(monkeypatch) -> None:
    backend = _Backend(dict(_GRACE, proximity_lock_grace_sec=90))
    result, _ = _run(monkeypatch, backend, "enable")
    assert result.exit_code == 0
    assert "not an authentication factor" in result.output
    result, _ = _run(monkeypatch, backend, "enable", "--grace", "120")
    result, _ = _run(monkeypatch, backend, "disable")
    assert backend.set_calls == [(True, 90), (True, 120), (False, 90)]


def test_enable_rejects_out_of_range_grace(monkeypatch) -> None:
    backend = _Backend(_GRACE)
    result, _ = _run(monkeypatch, backend, "enable", "--grace", "5")
    assert result.exit_code != 0
    assert backend.set_calls == []
