"""The Quickshell pairing adapter uses a line-oriented confirmation protocol."""

from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from blueferry import cli, pairing_cli, setup_client


def test_pair_setup_debug_enables_diagnostic_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_setup_logging", lambda enabled: calls.append(("debug", enabled)))
    monkeypatch.setattr(
        pairing_cli,
        "run_wizard",
        lambda *, verify_after: calls.append(("wizard", verify_after)) or 0,
    )

    result = CliRunner().invoke(cli.app, ["pair-setup", "--debug", "--no-verify"])

    assert result.exit_code == 0
    assert calls == [("debug", True), ("wizard", False)]


def test_interactive_pairing_emits_code_and_waits_for_acceptance(monkeypatch):
    observed = []

    class Setup:
        def prepare_replacement(self, previous_mac, next_mac):
            observed.append(("replace", previous_mac, next_mac))

        def complete(self, mac, *, confirmation, display):
            observed.append((mac, confirmation(12345)))
            return SimpleNamespace(to_dict=lambda: {"ok": True, "device": {"mac": mac}})

    monkeypatch.setattr(setup_client, "SetupClient", Setup)

    result = CliRunner().invoke(
        cli.app,
        [
            "pairing-complete",
            "02:00:00:00:00:01",
            "--interactive-agent",
            "--replace-saved-mac",
            "02:00:00:00:00:02",
        ],
        input="yes\n",
    )

    assert result.exit_code == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events == [
        {"event": "confirmation", "passkey": "012345"},
        {"ok": True, "device": {"mac": "02:00:00:00:00:01"}},
    ]
    assert observed == [
        ("replace", "02:00:00:00:00:02", "02:00:00:00:00:01"),
        ("02:00:00:00:00:01", True),
    ]
