"""`blueferry storage-reset` talks only to a fake backend client."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from blueferry import cli
from blueferry.client import BackendError


class _Backend:
    def __init__(self, error: str = "") -> None:
        self.error = error
        self.resets = 0

    def reset_storage(self) -> dict:
        self.resets += 1
        if self.error:
            raise BackendError(self.error)
        return {"storage_policy": "encrypted", "storage_state": "ready", "storage_detail": ""}


@pytest.fixture
def backend(monkeypatch):
    fake = _Backend()
    monkeypatch.setattr(cli, "_backend_client", lambda: fake)
    return fake


def test_reset_asks_first_and_does_nothing_when_declined(backend) -> None:
    result = CliRunner().invoke(cli.app, ["storage-reset"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Nothing on the iPhone is deleted" in result.output
    assert backend.resets == 0


@pytest.mark.parametrize(("args", "answer"), [((), "y\n"), (("--yes",), None)])
def test_reset_reports_the_new_storage_status(backend, args, answer) -> None:
    result = CliRunner().invoke(cli.app, ["storage-reset", *args], input=answer)

    assert result.exit_code == 0, result.output
    assert backend.resets == 1
    assert json.loads(result.output.splitlines()[-1])["storage_state"] == "ready"


def test_reset_failure_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_backend_client", lambda: _Backend("a desktop wallet request is already pending"))

    result = CliRunner().invoke(cli.app, ["storage-reset", "--yes"])

    assert result.exit_code == 2
    assert "already pending" in result.output
