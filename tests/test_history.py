from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from blueferry.history import (
    append_event,
    clear_events,
    history_count,
    history_revision,
    minimize_ancs_history,
    prune_events,
    read_events,
)


def test_reader_filters_and_ignores_corrupt_payload_rows(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    append_event({"kind": "sms_received", "body": "one"}, path=path)
    append_event({"kind": "ancs_notification", "body": "two"}, path=path)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(
            "INSERT INTO events(kind, payload_json) VALUES (?, ?)",
            ("sms_received", "not json"),
        )
    assert read_events(path=path, kinds={"sms_received"}) == [
        {"kind": "sms_received", "body": "one"}
    ]
    assert history_count(path=path) == 3
    assert history_count(path=path, kinds={"ancs_notification"}) == 1


def test_prune_enforces_age_and_count_transactionally(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    now = datetime.now(timezone.utc)
    events = [
        {
            "kind": "sms_received",
            "body": "old",
            "seen_at": (now - timedelta(days=31)).isoformat(),
        },
        *[
            {
                "kind": "sms_received",
                "body": str(index),
                "seen_at": (now - timedelta(minutes=index)).isoformat(),
            }
            for index in range(5)
        ],
    ]
    for event in events:
        append_event(event, path=path)

    assert prune_events(path=path, retention_days=30, max_events=3) == 3
    assert [event["body"] for event in read_events(path=path)] == ["2", "3", "4"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_prune_enforces_serialized_payload_budget(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    bodies = [character * 100 for character in "abc"]
    for body in bodies:
        append_event({"kind": "sms_received", "body": body}, path=path)

    # One serialized event fits; two do not. The newest retained event wins.
    assert prune_events(
        path=path,
        retention_days=30,
        max_events=100,
        max_payload_bytes=180,
    ) == 2
    assert [event["body"] for event in read_events(path=path)] == [bodies[-1]]


def test_clear_preserves_private_database_and_changes_revision(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    append_event({"kind": "sms_received", "body": "secret"}, path=path)
    before = history_revision(path=path)

    clear_events(path=path)

    assert read_events(path=path) == []
    assert history_revision(path=path) != before
    assert path.stat().st_mode & 0o777 == 0o600


def test_reader_can_bound_bodies_for_presentation_without_changing_archive(
    tmp_path,
) -> None:
    path = tmp_path / "events.sqlite"
    append_event({"kind": "sms_received", "body": "abcdefgh"}, path=path)

    projected = read_events(path=path, max_body_chars=4)

    assert projected[0]["body"] == "abc…"
    assert projected[0]["body_truncated"] is True
    assert read_events(path=path)[0]["body"] == "abcdefgh"


def test_ancs_history_retains_only_minimal_messages_correlation_data(
    tmp_path,
) -> None:
    path = tmp_path / "events.sqlite"
    append_event({
        "kind": "ancs_notification",
        "app_id": "com.example.Private",
        "title": "Secret system title",
        "body": "Secret system body",
    }, path=path)
    append_event({
        "kind": "ancs_notification",
        "notification_id": 7,
        "device_path": "/private/device/path",
        "app_id": "com.apple.MobileSMS",
        "app_name": "Messages",
        "title": "Alice",
        "subtitle": "To you & Bob",
        "body": "hello",
        "category": "Social",
        "seen_at": "2026-08-08T12:00:00+00:00",
    }, path=path)

    assert minimize_ancs_history(path=path) == (1, 1)

    retained = read_events(path=path)
    assert retained == [{
        "kind": "ancs_notification",
        "notification_id": 7,
        "app_id": "com.apple.MobileSMS",
        "title": "Alice",
        "subtitle": "To you & Bob",
        "body": "hello",
        "seen_at": "2026-08-08T12:00:00+00:00",
    }]
