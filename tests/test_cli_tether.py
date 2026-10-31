"""``blueferry tether`` presentation with an injected backend client."""
from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from blueferry import cli, cli_tether
from blueferry.client import BackendError
from blueferry.tether_status import TetherStatus, tether_error_hint

runner = CliRunner()


class _Client:
    def __init__(self, states, *, error=None, enabled=True) -> None:
        self.states = list(states)
        self.error = error
        self.enabled = enabled
        self.calls: list[str] = []

    def _next(self, name):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return TetherStatus.from_dict({"enabled": self.enabled, **state})

    def tether_configure(self, enabled, autoconnect):
        self.calls.append(f"configure:{enabled}:{autoconnect}")
        self.enabled = enabled
        return self._next_after_configure(autoconnect)

    def _next_after_configure(self, autoconnect):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return TetherStatus.from_dict(
            {"enabled": self.enabled, "autoconnect": autoconnect, **state}
        )

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


@pytest.mark.parametrize(("action", "state"), [("off", "disconnecting"), ("on", "connecting")])
def test_without_wait_an_accepted_request_exits_0(fake, action, state) -> None:
    client = fake(_Client([{"state": state}]))
    result = runner.invoke(cli.app, ["tether", action, "--wait", "0"])
    assert client.calls == ["disconnect" if action == "off" else "connect"]
    assert result.exit_code == 0


def test_without_wait_a_failed_request_exits_1(fake) -> None:
    fake(_Client([{"state": "failed", "error": "hotspot-refused"}]))
    result = runner.invoke(cli.app, ["tether", "on", "--wait", "0"])
    assert result.exit_code == 1


def test_help_documents_the_exit_status(fake) -> None:
    # Under GitHub Actions Typer forces Rich into terminal mode, so with Rich
    # installed the help has ANSI codes and panel borders inside wrapped
    # lines. Compare the plain words.
    result = runner.invoke(
        cli.app,
        ["tether", "--help"],
        env={"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"},
    )
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    text = " ".join(plain.replace("\u2502", " ").split())
    assert "Exit status: 0" in text
    assert "1 when it was not reached" in text
    assert "2 when the request was rejected" in text


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


class _DisabledClient(_Client):
    """A daemon with tethering off: Connect is refused with NotReady."""

    def tether_connect(self):
        self.calls.append("connect")
        raise BackendError(
            "Bluetooth tethering is turned off; enable it in BlueFerry's "
            "iPhone settings or with 'blueferry tether enable'"
        )


def test_on_while_disabled_is_refused_with_a_clear_message(fake) -> None:
    client = fake(_DisabledClient([{"state": "off"}], enabled=False))
    result = runner.invoke(cli.app, ["tether", "on"])
    assert result.exit_code == 2
    assert client.calls == ["connect", "state"]
    assert "turned off" in result.output
    assert "blueferry tether enable" in result.output


def test_other_refusals_keep_the_daemon_message(fake) -> None:
    class _NotConnected(_Client):
        def tether_connect(self):
            self.calls.append("connect")
            raise BackendError("the iPhone is not connected over Bluetooth yet")

    fake(_NotConnected([{"state": "off"}], enabled=True))
    result = runner.invoke(cli.app, ["tether", "on"])
    assert result.exit_code == 2
    assert "not connected over Bluetooth" in result.output
    assert "blueferry tether enable" not in result.output


def test_status_while_disabled_says_so(fake) -> None:
    fake(_Client([{"state": "off"}], enabled=False))
    result = runner.invoke(cli.app, ["tether"])
    assert result.exit_code == 0
    assert "turned off" in result.output


def test_enable_keeps_the_saved_automatic_choice(fake) -> None:
    client = fake(_Client([{"state": "off", "autoconnect": True}], enabled=False))
    result = runner.invoke(cli.app, ["tether", "enable"])
    assert result.exit_code == 0, result.output
    assert client.calls == ["state", "configure:True:True"]
    assert "Not sharing" in result.output


@pytest.mark.parametrize(("flag", "expected"), [
    ("--autoconnect", True), ("--no-autoconnect", False),
])
def test_enable_can_set_automatic_tethering(fake, flag, expected) -> None:
    client = fake(_Client([{"state": "off"}], enabled=False))
    result = runner.invoke(cli.app, ["tether", "enable", flag, "--json"])
    assert result.exit_code == 0, result.output
    assert client.calls == [f"configure:True:{expected}"]
    payload = json.loads(result.output)
    assert (payload["enabled"], payload["autoconnect"]) == (True, expected)


def test_disable_waits_for_our_own_link_to_stop(fake) -> None:
    client = fake(_Client([{"state": "off"}, {"state": "disconnecting"}, {"state": "off"}]))
    result = runner.invoke(cli.app, ["tether", "disable"])
    assert result.exit_code == 0, result.output
    assert client.calls == ["state", "configure:False:False", "state"]
    assert "turned off" in result.output


def test_autoconnect_flag_is_only_for_enable(fake) -> None:
    client = fake(_Client([{"state": "off"}]))
    result = runner.invoke(cli.app, ["tether", "on", "--autoconnect"])
    assert result.exit_code == 2
    assert client.calls == []


def test_disabled_summary_is_shared_by_every_client() -> None:
    off = TetherStatus.from_dict({"state": "off", "enabled": False})
    assert "turned off" in off.summary()
    # A stop that is still in flight after disabling is still described.
    stopping = TetherStatus.from_dict({"state": "disconnecting", "enabled": False})
    assert "Disconnecting" in stopping.summary()
