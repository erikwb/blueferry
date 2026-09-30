"""Persistent, policy-aware store for the mirrored iPhone call history.

PBAP transport lives in :mod:`blueferry.call_history_sync` and parsing in
:mod:`blueferry.call_history`; this module exclusively owns the SQLite schema,
the replacement transaction, retention, and the record of which missed calls
were already announced.

Every row, including the announcement state, is sealed with the local storage
policy exactly like the contact cache: AES-256-GCM under the keyring key when
encrypted storage is selected. The database never holds a queryable number,
name, direction, or timestamp column.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blueferry import config
from blueferry.call_history import MISSED, CallRecord
from blueferry.limits import MAX_CALL_HISTORY_RECORDS, MAX_CALL_HISTORY_SEEN_KEYS
from blueferry.storage_security import CorruptStorageError, StorageSecurity

log = logging.getLogger(__name__)

_RECORD_PURPOSE = "call-record-v1"
_STATE_PURPOSE = "call-state-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    name    TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Outcome of one mirrored sync."""

    records: list[CallRecord]
    # Missed calls not seen by any earlier sync. Empty on the seeding sync.
    new_missed: list[CallRecord] = field(default_factory=list)
    seeded: bool = False
    changed: bool = False


def _cutoff(now: datetime, retention_days: int | None) -> datetime:
    days = retention_days or config.HISTORY_RETENTION_DAYS
    return now - timedelta(days=max(1, int(days)))


class CallHistoryRepository:
    """Replace, load, prune, and clear one call-history mirror."""

    def __init__(
        self, storage: StorageSecurity | None, *, path: Path | None = None,
    ) -> None:
        self.storage = storage
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or config.CALLS_DB

    def _open(self) -> sqlite3.Connection:
        target = self.path
        if target.parent == config.STATE_DIR:
            config.ensure_dirs()
        with config.open_state_file(target, "a"):
            pass
        connection = sqlite3.connect(target, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            # Retention and clearing are privacy operations: overwrite freed
            # cells rather than leaving them for forensic recovery.
            connection.execute("PRAGMA secure_delete = ON")
            connection.executescript(_SCHEMA)
            target.chmod(config.STATE_FILE_MODE)
            return connection
        except Exception:
            connection.close()
            raise

    # ---- encoding ------------------------------------------------------

    def _seal(self, value: object, purpose: str) -> str:
        if self.storage is None:
            raise RuntimeError("call history requires local storage")
        return self.storage.encrypt(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            purpose=purpose,
        )

    def _open_value(self, payload: object, purpose: str) -> object:
        if self.storage is None:
            raise RuntimeError("call history requires local storage")
        return json.loads(self.storage.decrypt(str(payload), purpose=purpose))

    def _fail_closed(self) -> None:
        if self.storage is not None:
            self.storage.fail_closed("Encrypted call history could not be authenticated")

    # ---- reads ---------------------------------------------------------

    def _read_records(self, connection: sqlite3.Connection) -> list[CallRecord] | None:
        records: list[CallRecord] = []
        for (payload,) in connection.execute(
            "SELECT payload FROM calls ORDER BY id LIMIT ?",
            (MAX_CALL_HISTORY_RECORDS,),
        ):
            try:
                record = CallRecord.from_storage(
                    self._open_value(payload, _RECORD_PURPOSE)
                )
            except CorruptStorageError:
                self._fail_closed()
                return None
            except (ValueError, TypeError, RuntimeError):
                continue
            if record is not None:
                records.append(record)
        return records

    def _read_seen(self, connection: sqlite3.Connection) -> dict[str, float] | None:
        """Return announced missed-call keys, or ``None`` before the first sync."""
        row = connection.execute(
            "SELECT payload FROM state WHERE name = 'seen'"
        ).fetchone()
        if row is None:
            return None
        value = self._open_value(row[0], _STATE_PURPOSE)
        if not isinstance(value, dict):
            # Unusable state: seed again silently rather than treating every
            # retained missed call as new.
            return None
        return {
            str(key): float(stamp)
            for key, stamp in value.items()
            if isinstance(key, str) and isinstance(stamp, int | float)
        }

    def load(
        self, *, now: datetime | None = None, retention_days: int | None = None,
    ) -> list[CallRecord]:
        """Retained records newest-first; expired rows are never returned."""
        if self.storage is None or not self.storage.status.can_read:
            return []
        if not self.path.exists():
            return []
        cutoff = _cutoff(now or datetime.now(timezone.utc), retention_days)
        with closing(self._open()) as connection:
            records = self._read_records(connection)
        if records is None:
            return []
        kept = [record for record in records if record.occurred_at >= cutoff]
        kept.sort(key=lambda record: (record.occurred_at, record.key), reverse=True)
        return kept

    # ---- writes --------------------------------------------------------

    def replace(
        self,
        records: Sequence[CallRecord],
        *,
        now: datetime | None = None,
        retention_days: int | None = None,
    ) -> ReplaceResult:
        """Mirror the phone's lists and report missed calls not seen before.

        The first sync (no announcement state yet, including after the user
        cleared history or changed the storage policy) seeds the state
        silently, so enabling the feature never floods the desktop with the
        phone's whole missed-call backlog.
        """
        if self.storage is None or not self.storage.status.can_write:
            raise RuntimeError(
                self.storage.status.detail if self.storage is not None
                else "call history requires local storage"
            )
        current = now or datetime.now(timezone.utc)
        cutoff = _cutoff(current, retention_days)
        kept = sorted(
            (record for record in records if record.occurred_at >= cutoff),
            key=lambda record: (record.occurred_at, record.key),
            reverse=True,
        )[:MAX_CALL_HISTORY_RECORDS]
        with closing(self._open()) as connection:
            try:
                previous = self._read_records(connection)
                seen = self._read_seen(connection)
            except CorruptStorageError:
                self._fail_closed()
                raise
            if previous is None:
                raise CorruptStorageError("retained call history failed authentication")
            seeded = seen is None
            known = dict(seen or {})
            missed = [record for record in kept if record.direction == MISSED]
            new_missed = [] if seeded else [
                record for record in missed if record.key not in known
            ]
            for record in missed:
                known[record.key] = record.occurred_at.timestamp()
            floor = cutoff.timestamp()
            retained_seen = dict(sorted(
                ((key, stamp) for key, stamp in known.items() if stamp >= floor),
                key=lambda item: item[1],
                reverse=True,
            )[:MAX_CALL_HISTORY_SEEN_KEYS])
            # Rows are stored oldest-first; ``kept`` is newest-first.
            changed = [record.to_storage() for record in previous] != [
                record.to_storage() for record in reversed(kept)
            ]
            seen_changed = retained_seen != seen
            # An unchanged poll (the common case every few minutes) must not
            # rewrite and re-encrypt the whole mirror.
            if changed or seen_changed:
                with connection:
                    if changed:
                        connection.execute("DELETE FROM calls")
                        # Insert oldest-first so row order matches time order.
                        for record in reversed(kept):
                            connection.execute(
                                "INSERT INTO calls(payload) VALUES (?)",
                                (self._seal(record.to_storage(), _RECORD_PURPOSE),),
                            )
                    if seen_changed:
                        connection.execute(
                            "INSERT INTO state(name, payload) VALUES ('seen', ?) "
                            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload",
                            (self._seal(retained_seen, _STATE_PURPOSE),),
                        )
        return ReplaceResult(
            records=kept, new_missed=new_missed, seeded=seeded, changed=changed,
        )

    def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None,
    ) -> int:
        """Delete rows older than the retention window; return the count."""
        if self.storage is None or not self.storage.status.can_write:
            return 0
        if not self.path.exists():
            return 0
        cutoff = _cutoff(now or datetime.now(timezone.utc), retention_days)
        expired: list[int] = []
        with closing(self._open()) as connection:
            for row_id, payload in connection.execute("SELECT id, payload FROM calls"):
                try:
                    record = CallRecord.from_storage(
                        self._open_value(payload, _RECORD_PURPOSE)
                    )
                except CorruptStorageError:
                    self._fail_closed()
                    return 0
                except (ValueError, TypeError, RuntimeError):
                    record = None
                if record is None or record.occurred_at < cutoff:
                    expired.append(int(row_id))
            if expired:
                with connection:
                    connection.executemany(
                        "DELETE FROM calls WHERE id = ?",
                        ((row_id,) for row_id in expired),
                    )
        return len(expired)

    def clear(self) -> None:
        """Erase every retained call and the announcement state.

        Clearing the state means the next sync seeds silently again.
        """
        if not self.path.exists():
            return
        with closing(self._open()) as connection:
            with connection:
                connection.execute("DELETE FROM calls")
                connection.execute("DELETE FROM state")
            connection.execute("VACUUM")


def clear_call_history(path: Path | None = None) -> None:
    """Erase the call-history mirror without needing a storage key."""
    CallHistoryRepository(None, path=path).clear()
