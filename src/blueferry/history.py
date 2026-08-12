"""Bounded private event history backed by SQLite."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blueferry import config
from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.storage_security import (
    ENCRYPTED_STORAGE,
    CorruptStorageError,
    is_encrypted_value,
)

if TYPE_CHECKING:
    from blueferry.storage_security import StorageSecurity

EventRecord = dict[str, Any]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    occurred_at  REAL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind_id ON events(kind, id);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
"""


def _event_datetime(event: EventRecord) -> datetime | None:
    raw = str(event.get("seen_at") or event.get("timestamp") or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def _serialize(event: EventRecord, storage: StorageSecurity | None) -> str:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (
        storage.encrypt(payload, purpose="history-event-v1")
        if storage is not None else payload
    )


def _deserialize(
    payload: str, storage: StorageSecurity | None
) -> EventRecord | None:
    if storage is not None:
        payload = storage.decrypt(payload, purpose="history-event-v1")
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def _insert(
    database: sqlite3.Connection,
    event: EventRecord,
    storage: StorageSecurity | None,
) -> None:
    occurred = _event_datetime(event)
    # Keep queryable metadata only for direct test stores. Policy-managed
    # stores keep it inside the payload even when the user chooses plaintext,
    # so changing protection does not change the database's visible shape.
    stored_kind = (
        str(event.get("kind") or "unknown") if storage is None else "private"
    )
    database.execute(
        "INSERT INTO events(kind, occurred_at, payload_json) VALUES (?, ?, ?)",
        (
            stored_kind,
            occurred.timestamp()
            if storage is None and occurred is not None else None,
            _serialize(event, storage),
        ),
    )


def _open_database(path: Path | None = None) -> sqlite3.Connection:
    target = path or config.EVENTS_DB
    if target.parent == config.STATE_DIR:
        config.ensure_dirs()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    with config.open_state_file(target, "a"):
        pass
    database = sqlite3.connect(target, timeout=5.0)
    try:
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA busy_timeout = 5000")
        database.executescript(_SCHEMA)
        target.chmod(config.STATE_FILE_MODE)
        return database
    except Exception:
        database.close()
        raise


def append_event(
    event: EventRecord,
    *,
    path: Path | None = None,
    storage: StorageSecurity | None = None,
) -> None:
    """Commit one serialized event as a single SQLite transaction."""
    with closing(_open_database(path)) as database, database:
        _insert(database, event, storage)


def read_events(
    *,
    kinds: set[str] | None = None,
    limit: int | None = None,
    path: Path | None = None,
    max_body_chars: int | None = None,
    storage: StorageSecurity | None = None,
) -> list[EventRecord]:
    """Read valid events oldest-first, optionally filtering and tail-limiting.

    ``max_body_chars`` bounds presentation projections without altering the
    private archive. A caller requesting raw history leaves it unset.
    """
    if storage is not None and not storage.status.can_read:
        return []
    bounded = max(0, int(limit)) if limit is not None else None
    if bounded == 0:
        return []
    events: list[EventRecord] = []
    with closing(_open_database(path)) as database:
        for (payload,) in database.execute(
            "SELECT payload_json FROM events ORDER BY id"
        ):
            try:
                event = _deserialize(payload, storage)
            except CorruptStorageError:
                if storage is not None:
                    storage.fail_closed(
                        "Encrypted local history could not be authenticated"
                    )
                return []
            except (ValueError, RuntimeError):
                continue
            if event is not None and (
                not kinds or str(event.get("kind") or "") in kinds
            ):
                body = event.get("body")
                if (
                    max_body_chars is not None
                    and isinstance(body, str)
                    and len(body) > max_body_chars
                ):
                    body_limit = max(0, int(max_body_chars))
                    event["body"] = (
                        body[:max(0, body_limit - 1)]
                        + ("…" if body_limit else "")
                    )
                    event["body_truncated"] = True
                events.append(event)
    return events[-bounded:] if bounded is not None else events


def history_revision(
    *, path: Path | None = None, storage: StorageSecurity | None = None
) -> tuple[int, int]:
    """Return a cheap cache key that changes for every append or deletion."""
    if storage is not None and not storage.status.can_read:
        return 0, 0
    with closing(_open_database(path)) as database:
        count, newest = database.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM events"
        ).fetchone()
    return int(count), int(newest)


def history_count(
    *,
    kinds: set[str] | None = None,
    path: Path | None = None,
    storage: StorageSecurity | None = None,
) -> int:
    """Count events without loading private payloads into Python."""
    if storage is not None and not storage.status.can_read:
        return 0
    if kinds:
        return len(read_events(kinds=kinds, path=path, storage=storage))
    with closing(_open_database(path)) as database:
        return int(database.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def prune_events(
    *,
    path: Path | None = None,
    retention_days: int | None = None,
    max_events: int | None = None,
    max_payload_bytes: int | None = None,
    storage: StorageSecurity | None = None,
) -> int:
    """Transactionally enforce history age and count limits."""
    if storage is not None and not storage.status.can_read:
        return 0
    selected_max = max_events or config.HISTORY_MAX_EVENTS
    selected_bytes = max_payload_bytes or config.HISTORY_MAX_PAYLOAD_BYTES
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=retention_days or config.HISTORY_RETENTION_DAYS
    )
    with closing(_open_database(path)) as database, database:
        before = int(database.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        if storage is not None and before:
            # Verify that the archive is readable before retention deletion.
            # In encrypted mode, a replaced wallet key must never make an
            # otherwise intact archive look like disposable old data.
            (newest_payload,) = database.execute(
                "SELECT payload_json FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            try:
                _deserialize(newest_payload, storage)
            except CorruptStorageError:
                storage.fail_closed(
                    "Encrypted local history could not be authenticated"
                )
                return 0
            except (ValueError, RuntimeError):
                return 0
        # Enforce the cheap cardinality ceiling before decrypting timestamps,
        # so even a damaged or locally inflated database cannot force an
        # unbounded startup scan.
        database.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (selected_max,),
        )
        expired: list[int] = []
        validated: list[int] = []
        for event_id, payload in database.execute(
            "SELECT id, payload_json FROM events ORDER BY id"
        ):
            try:
                event = _deserialize(payload, storage)
            except CorruptStorageError:
                if storage is not None:
                    storage.fail_closed(
                        "Encrypted local history could not be authenticated"
                    )
                return 0
            except (ValueError, RuntimeError):
                continue
            if storage is not None:
                validated.append(int(event_id))
            occurred = _event_datetime(event) if event is not None else None
            if occurred is not None and occurred < cutoff:
                expired.append(int(event_id))
        if expired:
            database.executemany(
                "DELETE FROM events WHERE id = ?",
                ((event_id,) for event_id in expired),
            )
        if validated:
            database.executemany(
                "UPDATE events SET kind = 'private', occurred_at = NULL "
                "WHERE id = ?",
                ((event_id,) for event_id in validated),
            )
        total = 0
        delete_through = None
        for event_id, payload_size in database.execute(
            "SELECT id, length(CAST(payload_json AS BLOB)) "
            "FROM events ORDER BY id DESC"
        ):
            size = int(payload_size or 0)
            if total + size > selected_bytes:
                delete_through = int(event_id)
                break
            total += size
        if delete_through is not None:
            database.execute("DELETE FROM events WHERE id <= ?", (delete_through,))
        after = int(database.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    return before - after


def clear_events(*, path: Path | None = None) -> None:
    """Erase local history and scrub SQLite's reusable pages."""
    with closing(_open_database(path)) as database:
        database.execute("PRAGMA secure_delete = ON")
        with database:
            database.execute("DELETE FROM events")
        database.execute("VACUUM")


def scrub_unprotected_events(
    *, path: Path | None = None, storage: StorageSecurity
) -> int:
    """Remove legacy plaintext rows before encrypted history is read.

    Ciphertext with a valid BlueFerry frame is retained even if its key no
    longer works. That distinction avoids turning a keyring problem into data
    loss while ensuring plaintext left by an older policy is erased.
    """
    if (
        storage.status.policy != ENCRYPTED_STORAGE
        or not storage.status.can_read
    ):
        return 0
    with closing(_open_database(path)) as database:
        plaintext_ids = [
            int(event_id)
            for event_id, payload in database.execute(
                "SELECT id, payload_json FROM events"
            )
            if not is_encrypted_value(str(payload))
        ]
        if not plaintext_ids:
            return 0
        database.execute("PRAGMA secure_delete = ON")
        with database:
            database.executemany(
                "DELETE FROM events WHERE id = ?",
                ((event_id,) for event_id in plaintext_ids),
            )
        database.execute("VACUUM")
    return len(plaintext_ids)


_ANCS_CORRELATION_FIELDS = (
    "kind",
    "notification_id",
    "app_id",
    "title",
    "subtitle",
    "body",
    "seen_at",
)


def minimize_ancs_history(
    *, path: Path | None = None, storage: StorageSecurity | None = None
) -> tuple[int, int]:
    """Delete unrelated ANCS content and compact Messages correlation rows.

    Returns ``(deleted, minimized)``. This is an ongoing privacy invariant,
    not a versioned migration: every daemon startup reasserts it before ANCS
    history is loaded.
    """
    if storage is not None and not storage.status.can_read:
        return 0, 0
    deleted_ids: list[int] = []
    minimized: list[tuple[str, int]] = []
    with closing(_open_database(path)) as database, database:
        rows = database.execute(
            "SELECT id, payload_json FROM events ORDER BY id"
        )
        for event_id, payload in rows:
            try:
                event = _deserialize(payload, storage)
            except CorruptStorageError:
                if storage is not None:
                    storage.fail_closed(
                        "Encrypted local history could not be authenticated"
                    )
                return 0, 0
            except (ValueError, RuntimeError):
                continue
            if event is None or event.get("kind") != "ancs_notification":
                continue
            if event.get("app_id") != MESSAGES_APP_ID:
                deleted_ids.append(int(event_id))
                continue
            projection = {
                key: event.get(key)
                for key in _ANCS_CORRELATION_FIELDS
            }
            if event != projection:
                serialized = _serialize(projection, storage)
                minimized.append((serialized, int(event_id)))
        if deleted_ids:
            database.executemany(
                "DELETE FROM events WHERE id = ?",
                ((event_id,) for event_id in deleted_ids),
            )
        if minimized:
            database.executemany(
                "UPDATE events SET payload_json = ? WHERE id = ?",
                minimized,
            )
    return len(deleted_ids), len(minimized)
