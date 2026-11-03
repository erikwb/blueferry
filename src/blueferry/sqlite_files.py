"""Recognize and discard SQLite files that SQLite itself cannot read."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# SQLITE_CORRUPT and SQLITE_NOTADB: the file is damaged, not merely busy,
# locked, or briefly unreadable.
_DAMAGED_CODES = frozenset({11, 26})
_DAMAGED_MESSAGES = frozenset({"database disk image is malformed", "file is not a database"})
_SIDECARS = ("-journal", "-wal", "-shm")


def is_damaged(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xff in _DAMAGED_CODES
    # Python 3.10 has no error codes: fall back to SQLite's messages.
    return str(error).casefold() in _DAMAGED_MESSAGES


def remove_database(path: Path) -> None:
    """Delete a database and its journal files; the next open starts afresh."""
    # Journals first, so nothing can roll an old journal into a new file.
    for suffix in _SIDECARS:
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    log.warning("removed unreadable database %s", path.name)
