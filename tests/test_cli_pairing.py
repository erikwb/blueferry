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


def test_pairing_complete_failure_includes_report_path(monkeypatch):
    from blueferry.errors import PairingError

    class Setup:
        @staticmethod
        def complete(*_args, **_kwargs):
            raise PairingError(
                "adapter setup failed",
                report_path="/tmp/quirks-fail.json",
            )

    monkeypatch.setattr(setup_client, "SetupClient", Setup)

    result = CliRunner().invoke(
        cli.app, ["pairing-complete", "02:00:00:00:00:01"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "adapter setup failed"
    assert payload["report_path"] == "/tmp/quirks-fail.json"


def test_pairing_issue_prints_the_latest_report_without_opening(tmp_path, monkeypatch):
    from blueferry import config, quirks_report

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    opened = []
    path = quirks_report.save_report({"outcome": {"setup_complete": False}}, directory=tmp_path)
    monkeypatch.setattr(quirks_report, "open_issue_page", lambda: opened.append(True) or True)

    result = CliRunner().invoke(cli.app, ["pairing-issue", "--no-open"])

    assert result.exit_code == 0
    assert str(path) in result.stdout
    assert "iPhone model" in result.stdout
    assert "labels=pairing-issue" in result.stdout
    assert "title=" in result.stdout
    assert "body=" in result.stdout
    assert opened == []


def test_pairing_issue_print_url_is_only_the_github_link(tmp_path, monkeypatch):
    from blueferry import config, quirks_report

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    quirks_report.save_report(
        {
            "controller": {"name": "hci1", "vendor": "Intel"},
            "outcome": {"map": True, "pbap": False, "ancs": False},
        },
        directory=tmp_path,
    )
    opened = []
    monkeypatch.setattr(quirks_report, "open_issue_page", lambda *_args: opened.append(True))

    result = CliRunner().invoke(cli.app, ["pairing-issue", "--print-url"])

    assert result.exit_code == 0
    url = result.stdout.strip()
    assert url.startswith("https://github.com/erikwb/blueferry/issues/new?")
    assert "labels=pairing-issue" in url
    assert opened == []


def test_pairing_issue_without_a_report_exits_nonzero(tmp_path, monkeypatch):
    from blueferry import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    result = CliRunner().invoke(cli.app, ["pairing-issue", "--no-open"])
    assert result.exit_code == 1
    assert "No pairing report" in result.stdout
