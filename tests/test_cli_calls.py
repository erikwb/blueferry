"""`blueferry calls` CLI presentation over a fake backend client."""
from __future__ import annotations

from typer.testing import CliRunner

from blueferry import cli_calls
from blueferry.cli import app
from blueferry.client import BackendError
from blueferry.models import CallsSnapshot


class FakeClient:
    def __init__(self, calls=(), state="ready", error=None) -> None:
        self.snapshot = CallsSnapshot.from_dict({"state": state, "calls": list(calls)})
        self.error = error
        self.requests: list[tuple] = []

    def _record(self, *request):
        if self.error is not None:
            raise BackendError(self.error)
        self.requests.append(request)

    def calls(self):
        if self.error is not None:
            raise BackendError(self.error)
        return self.snapshot

    def dial(self, number):
        self._record("dial", number)
        return "voicecall05"

    def answer_call(self, call_id):
        self._record("answer", call_id)

    def hangup_call(self, call_id):
        self._record("hangup", call_id)

    def hangup_all_calls(self):
        self._record("hangup_all")

    def send_call_tones(self, call_id, tones):
        self._record("tones", call_id, tones)

    def swap_calls(self):
        self._record("swap")

    def hold_and_answer_call(self):
        self._record("hold_and_answer")


def _call(call_id="voicecall01", state="incoming", **extra):
    return {"call_id": call_id, "state": state, "direction": "incoming",
            "number": "+41791234567", "contact_name": "Alice", **extra}


def _invoke(monkeypatch, client, *args):
    monkeypatch.setattr(cli_calls, "_client", lambda: client)
    return CliRunner().invoke(app, ["calls", *args])


def test_listing_shows_state_and_escaped_callers(monkeypatch) -> None:
    client = FakeClient([_call(contact_name="Evil\x1b[2J")])

    result = _invoke(monkeypatch, client)

    assert result.exit_code == 0
    assert "Calls: ready" in result.output
    assert "voicecall01" in result.output and "+41791234567" in result.output
    assert "\x1b[2J" not in result.output


def test_disabled_listing_explains_the_opt_in(monkeypatch) -> None:
    result = _invoke(monkeypatch, FakeClient(error="phone calls are disabled"))

    assert result.exit_code == 3
    assert "phone calls are disabled" in result.output


def test_answer_defaults_to_the_single_ringing_call(monkeypatch) -> None:
    client = FakeClient([_call(), _call("voicecall02", "active")])

    result = _invoke(monkeypatch, client, "answer")

    assert result.exit_code == 0
    assert client.requests == [("answer", "voicecall01")]


def test_ambiguous_default_requires_an_explicit_call_id(monkeypatch) -> None:
    client = FakeClient([_call("voicecall01", "active"), _call("voicecall02", "held")])

    result = _invoke(monkeypatch, client, "hangup")

    assert result.exit_code == 2
    assert client.requests == []
    assert _invoke(monkeypatch, client, "hangup", "voicecall02").exit_code == 0
    assert client.requests == [("hangup", "voicecall02")]


def test_dial_dtmf_and_multi_call_commands(monkeypatch) -> None:
    client = FakeClient([_call(state="active")])

    assert _invoke(monkeypatch, client, "dial", "--yes", "+41 79 123 45 67").exit_code == 0
    assert _invoke(monkeypatch, client, "dtmf", "12#").exit_code == 0
    assert _invoke(monkeypatch, client, "hangup", "--all").exit_code == 0
    assert _invoke(monkeypatch, client, "swap").exit_code == 0
    assert _invoke(monkeypatch, client, "hold-answer").exit_code == 0

    assert client.requests == [
        ("dial", "+41 79 123 45 67"), ("tones", "voicecall01", "12#"),
        ("hangup_all",), ("swap",), ("hold_and_answer",),
    ]


def test_dtmf_without_an_active_call_fails_before_the_backend(monkeypatch) -> None:
    client = FakeClient([_call(state="incoming")])

    result = _invoke(monkeypatch, client, "dtmf", "1")

    assert result.exit_code == 2
    assert client.requests == []


def test_hangup_rejects_a_call_id_together_with_all(monkeypatch) -> None:
    client = FakeClient([_call(state="active")])

    result = _invoke(monkeypatch, client, "hangup", "voicecall01", "--all")

    assert result.exit_code == 2
    assert client.requests == []


def test_dial_asks_first_and_refuses_without_a_terminal(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(cli_calls, "_interactive", lambda: False)

    result = _invoke(monkeypatch, client, "dial", "0441234567")

    assert result.exit_code == 2 and "--yes" in result.output
    assert client.requests == []


def test_dial_confirmation_can_be_declined_or_accepted(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(cli_calls, "_client", lambda: client)
    monkeypatch.setattr(cli_calls, "_interactive", lambda: True)

    declined = CliRunner().invoke(app, ["calls", "dial", "0441234567"], input="n\n")
    assert declined.exit_code == 1 and client.requests == []
    assert "Call 0441234567 through the iPhone?" in declined.output

    accepted = CliRunner().invoke(app, ["calls", "dial", "0441234567"], input="y\n")
    assert accepted.exit_code == 0
    assert client.requests == [("dial", "0441234567")]
