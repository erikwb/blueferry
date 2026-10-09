"""The call-history command talks only to the backend client."""
from __future__ import annotations

from typing import ClassVar

import pytest
import typer
from typer.testing import CliRunner

from blueferry import cli, cli_call_history
from blueferry.client import BackendError
from blueferry.limits import MAX_CALL_HISTORY_QUERY_LIMIT
from blueferry.models import CallHistoryEntry


def _entries():
    rows = [
        {"direction": "outgoing", "timestamp": "2026-09-28T10:00:00+00:00",
         "address": "+15551230001", "name": "Anna", "contact_name": "Anna"},
        {"direction": "missed", "timestamp": "2026-09-28T09:00:00+00:00",
         "address": "+15551230002", "name": None, "contact_name": None},
        {"direction": "missed", "timestamp": "2026-09-28T08:00:00+00:00",
         "address": "\x1b[31m+1555", "name": "Eve\x1b]0;x\x07", "contact_name": None},
        {"direction": "voicemail", "timestamp": "2026-09-28T07:00:00+00:00"},
    ]
    return [entry for entry in map(CallHistoryEntry.from_dict, rows) if entry]


class _Backend:
    requested: ClassVar[list[int]] = []
    synced = 0

    def call_history(self, limit):
        type(self).requested.append(limit)
        return _entries()

    def sync_call_history(self):
        type(self).synced += 1
        return 3


@pytest.fixture
def backend(monkeypatch):
    _Backend.requested = []
    _Backend.synced = 0
    monkeypatch.setattr(cli_call_history, "BackendClient", _Backend)
    return _Backend


def test_missed_filter_fetches_the_full_window_and_neutralizes_controls(backend) -> None:
    result = CliRunner().invoke(cli.app, ["call-history", "--missed", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert backend.requested == [MAX_CALL_HISTORY_QUERY_LIMIT]
    assert backend.synced == 0
    lines = result.output.strip().splitlines()
    assert len(lines) == 1
    assert "+15551230002" in lines[0] and "missed" in lines[0]
    assert "\x1b" not in result.output


def test_default_listing_and_optional_sync(backend) -> None:
    result = CliRunner().invoke(cli.app, ["call-history", "--sync", "-n", "5"])

    assert result.exit_code == 0, result.output
    assert backend.synced == 1
    assert backend.requested == [5]
    assert "Anna" in result.output
    assert "voicemail" not in result.output  # unknown directions are dropped
    assert len(result.output.strip().splitlines()) == 3


def test_backend_errors_exit_with_a_message(monkeypatch) -> None:
    class _Disabled:
        def call_history(self, _limit):
            raise BackendError("call history is disabled")

    monkeypatch.setattr(cli_call_history, "BackendClient", _Disabled)

    with pytest.raises(typer.Exit) as raised:
        cli_call_history.call_history_list(missed=False, limit=5, sync=False)

    assert raised.value.exit_code == 3


def test_wire_decoder_keeps_valid_entries_and_unknown_fields() -> None:
    from blueferry.client_wire import decode_call_history

    decoded = decode_call_history(
        '[{"direction":"missed","timestamp":"2026-09-28T09:00:00+00:00",'
        '"address":"+1555","name":null,"contact_name":null,"future":1},'
        '{"direction":"missed"}, 7, {"timestamp":"x"}]'
    )

    assert [entry.direction for entry in decoded] == ["missed"]
    assert decoded[0].extra == {"future": 1}
    assert decoded[0].display_caller == "+1555"
    assert decoded[0].to_dict()["missed"] is True


def test_enable_and_disable_change_the_saved_preference(monkeypatch) -> None:
    changes = []

    class _Settable:
        def set_call_history(self, enabled, popups):
            changes.append((enabled, popups))
            return {"call_history_enabled": enabled, "missed_call_notifications": popups}

    monkeypatch.setattr(cli_call_history, "BackendClient", _Settable)
    runner = CliRunner()

    enabled = runner.invoke(cli.app, ["call-history", "enable", "--no-popups"])
    disabled = runner.invoke(cli.app, ["call-history", "disable"])

    assert enabled.exit_code == 0 and disabled.exit_code == 0
    assert changes == [(True, False), (False, True)]
    assert "popups are off" in enabled.output
    assert "erased" in disabled.output


def test_enable_reports_a_backend_error(monkeypatch) -> None:
    class _Old:
        def set_call_history(self, _enabled, _popups):
            raise BackendError("UnknownMethod")

    monkeypatch.setattr(cli_call_history, "BackendClient", _Old)

    result = CliRunner().invoke(cli.app, ["call-history", "enable"])

    assert result.exit_code == 2
