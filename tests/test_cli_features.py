"""`blueferry features` with a fake backend client."""
from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli_features
from blueferry.cli import app
from blueferry.client import BackendError


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def features(self):
        return {"mark_read_on_dismiss": {
            "value": False, "source": "settings",
            "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": True,
        }}

    def set_feature(self, name, enabled):
        if name == "nope":
            raise BackendError("unknown feature")
        self.calls.append((name, enabled))
        return "restart-required"


def test_list_and_switch(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(cli_features, "_client", lambda: client)
    result = CliRunner().invoke(app, ["features"])
    assert result.exit_code == 0, result.output
    assert "mark_read_on_dismiss" in result.output and "restart pending" in result.output
    result = CliRunner().invoke(app, ["features", "off", "mark_read_on_dismiss"])
    assert "restart the BlueFerry service" in result.output
    assert client.calls == [("mark_read_on_dismiss", False)]
    result = CliRunner().invoke(app, ["features", "on", "nope"])
    assert result.exit_code == 2 and "unknown feature" in result.output
