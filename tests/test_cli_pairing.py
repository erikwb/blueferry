"""The Quickshell pairing adapter uses a line-oriented confirmation protocol."""

from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from blueferry import cli, setup_client


def test_interactive_pairing_emits_code_and_waits_for_acceptance(monkeypatch):
    observed = []

    class Setup:
        def complete(self, mac, *, confirmation, display):
            observed.append((mac, confirmation(12345)))
            return SimpleNamespace(to_dict=lambda: {"ok": True, "device": {"mac": mac}})

    monkeypatch.setattr(setup_client, "SetupClient", Setup)

    result = CliRunner().invoke(
        cli.app,
        ["pairing-complete", "02:00:00:00:00:01", "--interactive-agent"],
        input="yes\n",
    )

    assert result.exit_code == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events == [
        {"event": "confirmation", "passkey": "012345"},
        {"ok": True, "device": {"mac": "02:00:00:00:00:01"}},
    ]
    assert observed == [("02:00:00:00:00:01", True)]
