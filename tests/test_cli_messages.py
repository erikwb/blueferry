from __future__ import annotations

import pytest
import typer

from blueferry import cli_messages
from blueferry.models import EventRecord


@pytest.mark.parametrize("address", ["+12025550123", "0412345678", "friend@example.com"])
@pytest.mark.parametrize("multiple", [False, True])
def test_contact_message_preserves_the_saved_destination(monkeypatch, address, multiple):
    sent = []

    class Backend:
        def find_contacts(self, _query):
            matches = [("Friend", address)]
            return matches + [("Other Friend", "+61423456789")] if multiple else matches

        def send(self, destination, body):
            sent.append((destination, body))
            return "/fake/transfer"

    monkeypatch.setattr(cli_messages, "BackendClient", Backend)
    monkeypatch.setattr(cli_messages, "setup_logging", lambda _verbose: None)
    monkeypatch.setattr(cli_messages.typer, "prompt", lambda *_a, **_k: 1)
    cli_messages.sms_send("Friend", "hello", verbose=False)
    assert sent == [(address, "hello")]


def test_email_only_contact_filter_matches_live_sender(monkeypatch) -> None:
    class _Backend:
        @staticmethod
        def find_contacts(_query):
            return [("Alice", "Alice@icloud.com")]

    monkeypatch.setattr(cli_messages, "BackendClient", _Backend)

    identities, text = cli_messages._sender_filter("Alice")

    assert cli_messages._matches_sender(
        {"sender": "alice@icloud.com"}, identities, text
    )


def test_local_sms_list_excludes_ancs_notifications(monkeypatch) -> None:
    rendered = []
    requested = []

    class _Backend:
        @staticmethod
        def events(kinds, limit):
            requested.append((kinds, limit))
            selected = set(kinds)
            events = [
                {
                    "kind": "sms_received",
                    "body": "hello",
                    "sender_address": "+15551234567",
                    "seen_at": "2026-08-08T10:00:00+00:00",
                },
                {
                    "kind": "ancs_notification",
                    "body": "not a message",
                    "seen_at": "2026-08-08T10:01:00+00:00",
                },
            ]
            return [
                EventRecord.from_dict(event)
                for event in events if event["kind"] in selected
            ]

    monkeypatch.setattr(cli_messages, "BackendClient", _Backend)
    monkeypatch.setattr(
        cli_messages,
        "format_message_timestamp",
        lambda value: f"friendly:{value}",
    )
    monkeypatch.setattr(
        cli_messages,
        "_render",
        lambda sender, body, timestamp, **_kwargs: rendered.append(
            (sender, body, timestamp)
        ),
    )

    cli_messages.sms_list(
        n=20,
        source="local",
        folder="telecom/msg/INBOX",
        from_contact=None,
    )

    assert requested[0][0] == ["sms_received", "sms_sent"]
    assert [item[1] for item in rendered] == ["hello"]
    assert rendered[0][2] == "friendly:2026-08-08T10:00:00+00:00"


def test_sms_list_rejects_unknown_source_before_any_io(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_messages,
        "BackendClient",
        lambda: pytest.fail("invalid source reached backend I/O"),
    )

    with pytest.raises(typer.Exit) as raised:
        cli_messages.sms_list(
            n=20,
            source="typo",
            folder="telecom/msg/INBOX",
            from_contact=None,
        )

    assert raised.value.exit_code == 2
