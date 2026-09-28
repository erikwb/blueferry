"""``blueferry tether`` presentation with an injected backend client."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from blueferry import cli, cli_tether
from blueferry.client import BackendError
from blueferry.tether_status import TetherStatus, tether_error_hint

runner = CliRunner()


class _Client:
    def __init__(self, states, *, error=None) -> None:
        self.states = list(states)
        self.error = error
        self.calls: list[str] = []

    def _next(self, name):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return TetherStatus.from_dict(self.states.pop(0) if len(self.states) > 1 else self.states[0])

    def tether_state(self):
        return self._next("state")

    def tether_connect(self):
        return self._next("connect")

    def tether_disconnect(self):
        return self._next("disconnect")


@pytest.fixture
def fake(monkeypatch):
    clock = [0.0]

    def install(client):
        monkeypatch.setattr(cli_tether, "BackendClient", lambda: client)
        monkeypatch.setattr(cli_tether, "_sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        monkeypatch.setattr(cli_tether, "_clock", lambda: clock[0])
        return client

    return install


def test_status_is_read_only(fake) -> None:
    client = fake(_Client([{"state": "off"}]))
    result = runner.invoke(cli.app, ["tether"])
    assert result.exit_code == 0
    assert client.calls == ["state"]
    assert "Not sharing" in result.output


def test_on_waits_until_connected_and_explains_dhcp_for_the_bluez_fallback(fake) -> None:
    client = fake(_Client([
        {"state": "connecting"},
        {"state": "connecting"},
        {"state": "connected", "interface": "bnep0", "backend": "bluez", "needs_dhcp": True},
    ]))
    result = runner.invoke(cli.app, ["tether", "on"])
    assert result.exit_code == 0, result.output
    assert client.calls == ["connect", "state", "state"]
    assert "DHCP client on bnep0" in result.output


def test_on_reports_a_refusal_with_hotspot_guidance(fake) -> None:
    fake(_Client([{"state": "connecting"}, {"state": "failed", "error": "hotspot-refused"}]))
    result = runner.invoke(cli.app, ["tether", "on"])
    assert result.exit_code == 1
    assert "Personal Hotspot" in result.output


def test_on_gives_up_after_the_wait(fake) -> None:
    client = fake(_Client([{"state": "connecting"}]))
    result = runner.invoke(cli.app, ["tether", "on", "--wait", "3"])
    assert result.exit_code == 1
    assert client.calls.count("state") == 3


def test_off_without_wait_returns_immediately(fake) -> None:
    client = fake(_Client([{"state": "disconnecting"}]))
    result = runner.invoke(cli.app, ["tether", "off", "--wait", "0"])
    assert client.calls == ["disconnect"]
    assert result.exit_code == 1  # not yet off


def test_json_output_is_the_decoded_state(fake) -> None:
    fake(_Client([{"state": "connected", "interface": "bnep0", "backend": "networkmanager"}]))
    result = runner.invoke(cli.app, ["tether", "status", "--json"])
    assert json.loads(result.output)["interface"] == "bnep0"


def test_backend_errors_exit_2(fake) -> None:
    fake(_Client([{}], error=BackendError("the iPhone is not connected over Bluetooth yet")))
    result = runner.invoke(cli.app, ["tether", "on"])
    assert result.exit_code == 2
    assert "not connected over Bluetooth" in result.output


def test_unknown_action_is_rejected_before_contacting_the_backend(fake) -> None:
    client = fake(_Client([{}]))
    result = runner.invoke(cli.app, ["tether", "toggle"])
    assert result.exit_code == 2
    assert client.calls == []


def test_status_model_rejects_unexpected_values() -> None:
    status = TetherStatus.from_dict({
        "state": "rebooting", "interface": 7, "external": "yes", "error": "x" * 500,
    })
    assert status.state == "off"
    assert status.interface == ""
    assert status.external is False
    assert len(status.error) <= 64


def test_every_backend_token_has_specific_guidance() -> None:
    from blueferry import tether

    generic = tether_error_hint("unknown")
    for token in tether.ERROR_TOKENS - {tether.GENERIC_ERROR}:
        assert tether_error_hint(token) != generic, token
