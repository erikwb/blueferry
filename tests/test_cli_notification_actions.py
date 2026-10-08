"""`blueferry notification-actions` CLI with a fake backend."""
from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli_notification_actions
from blueferry.cli import app
from blueferry.client import BackendError
from blueferry.models import BackendStatus


class _Backend:
    def __init__(self, status: dict | None) -> None:
        self._status = status
        self.set_calls: list[bool] = []

    def status(self) -> BackendStatus:
        if self._status is None:
            raise BackendError("not running")
        return BackendStatus.from_dict(self._status)

    def set_ancs_notification_actions(self, enabled: bool) -> bool:
        self.set_calls.append(enabled)
        assert self._status is not None
        self._status["ancs_actions_preference"] = enabled
        return enabled


def _run(monkeypatch, backend, *args):
    monkeypatch.setattr(cli_notification_actions, "_client", lambda: backend)
    return CliRunner().invoke(app, ["notification-actions", *args])


_STATUS = {
    "daemon": True,
    "notification_policy": "all",
    "ancs_actions_preference": False,
    "notification_content_shown": True,
}


def test_status_without_a_running_service(monkeypatch) -> None:
    result = _run(monkeypatch, _Backend(None))
    assert result.exit_code == 2


def test_enable_and_disable_round_trip(monkeypatch) -> None:
    backend = _Backend(dict(_STATUS))

    result = _run(monkeypatch, backend, "enable")
    assert result.exit_code == 0
    assert "Notification actions: on" in result.output
    result = _run(monkeypatch, backend, "disable")
    assert "Notification actions: off" in result.output
    assert backend.set_calls == [True, False]


def test_status_explains_why_actions_are_inactive(monkeypatch) -> None:
    hidden = _Backend({**_STATUS, "ancs_actions_preference": True,
                       "notification_content_shown": False})
    assert "content is hidden" in _run(monkeypatch, hidden).output
    messages_only = _Backend({**_STATUS, "ancs_actions_preference": True,
                              "notification_policy": "messages"})
    assert "All iPhone Notifications" in _run(monkeypatch, messages_only).output
    old = _Backend({"daemon": True})
    assert "does not support" in _run(monkeypatch, old).output
