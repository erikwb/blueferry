"""Persistent contact-cache repository.

PBAP transport and vCard parsing live in :mod:`blueferry.contacts`; this
module exclusively owns the SQLite schema, replacement transaction, legacy
plaintext cleanup, and encrypted-record loading.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing

from blueferry import config
from blueferry.events import is_email_shaped
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_NAME_CHARS,
    MAX_PHONEBOOK_CONTACTS,
)
from blueferry.storage_security import CorruptStorageError, StorageSecurity

log = logging.getLogger(__name__)

ContactRecord = tuple[str | None, list[str], list[str]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name    TEXT NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phones (
    phone_norm   TEXT NOT NULL,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    UNIQUE(phone_norm, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_phones_norm ON phones(phone_norm);

CREATE TABLE IF NOT EXISTS emails (
    email        TEXT NOT NULL,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    UNIQUE(email, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_emails_address ON emails(email);

CREATE TABLE IF NOT EXISTS secure_contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload      TEXT NOT NULL
);
"""


def _open_db() -> sqlite3.Connection:
    config.ensure_dirs()
    with config.open_state_file(config.CONTACTS_DB, "a"):
        pass
    connection = sqlite3.connect(config.CONTACTS_DB)
    connection.executescript(_SCHEMA)
    return connection


class ContactRepository:
    """Replace and load one policy-aware contact cache."""

    def __init__(self, storage: StorageSecurity | None = None) -> None:
        self.storage = storage

    @staticmethod
    def _delete_all(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM phones")
        connection.execute("DELETE FROM emails")
        connection.execute("DELETE FROM contacts")
        connection.execute("DELETE FROM secure_contacts")

    def clear(self) -> None:
        with closing(_open_db()) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                self._delete_all(connection)
            connection.execute("VACUUM")

    def replace(self, records: list[ContactRecord]) -> int:
        now = time.time()
        with closing(_open_db()) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                self._delete_all(connection)
                for name, phones, emails in records:
                    if not name and not phones and not emails:
                        continue
                    if self.storage is not None:
                        payload = json.dumps(
                            {"name": name or "", "phones": phones, "emails": emails},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        connection.execute(
                            "INSERT INTO secure_contacts(payload) VALUES (?)",
                            (self.storage.encrypt(
                                payload, purpose="contact-record-v1"
                            ),),
                        )
                        continue
                    cursor = connection.execute(
                        "INSERT INTO contacts(full_name, updated_at) VALUES (?, ?)",
                        (name or "", now),
                    )
                    contact_id = cursor.lastrowid
                    for phone in phones:
                        connection.execute(
                            "INSERT OR IGNORE INTO phones"
                            "(phone_norm, contact_id) VALUES (?, ?)",
                            (phone, contact_id),
                        )
                    for email in emails:
                        connection.execute(
                            "INSERT OR IGNORE INTO emails"
                            "(email, contact_id) VALUES (?, ?)",
                            (email, contact_id),
                        )
            if self.storage is not None:
                connection.execute("VACUUM")
        return len(records)

    @staticmethod
    def _plaintext_records(connection: sqlite3.Connection) -> list[ContactRecord]:
        records: list[ContactRecord] = []
        for contact_id, name in connection.execute(
            "SELECT id, full_name FROM contacts ORDER BY id"
        ):
            phones = [str(row[0]) for row in connection.execute(
                "SELECT phone_norm FROM phones WHERE contact_id = ? ORDER BY rowid",
                (contact_id,),
            )]
            emails = [str(row[0]) for row in connection.execute(
                "SELECT email FROM emails WHERE contact_id = ? ORDER BY rowid",
                (contact_id,),
            )]
            records.append((str(name), phones, emails))
        return records

    @staticmethod
    def _discard_plaintext(connection: sqlite3.Connection) -> bool:
        count = int(connection.execute(
            "SELECT (SELECT COUNT(*) FROM contacts) + "
            "(SELECT COUNT(*) FROM phones) + "
            "(SELECT COUNT(*) FROM emails)"
        ).fetchone()[0])
        if not count:
            return False
        connection.execute("PRAGMA secure_delete = ON")
        with connection:
            connection.execute("DELETE FROM phones")
            connection.execute("DELETE FROM emails")
            connection.execute("DELETE FROM contacts")
        return True

    def _secure_records(self, connection: sqlite3.Connection) -> list[ContactRecord]:
        if self.storage is None or not self.storage.status.can_read:
            return []
        records: list[ContactRecord] = []
        for (payload,) in connection.execute(
            "SELECT payload FROM secure_contacts ORDER BY id LIMIT ?",
            (MAX_PHONEBOOK_CONTACTS,),
        ):
            try:
                plaintext = self.storage.decrypt(
                    str(payload), purpose="contact-record-v1"
                )
                value = json.loads(plaintext)
                if not isinstance(value, dict):
                    continue
                name = str(value.get("name") or "")[:MAX_CONTACT_NAME_CHARS]
                phones = [
                    str(item) for item in value.get("phones", [])
                    if isinstance(item, str)
                    and len(item) <= MAX_CONTACT_ADDRESS_CHARS
                ]
                emails = [
                    str(item).casefold() for item in value.get("emails", [])
                    if isinstance(item, str)
                    and len(item) <= MAX_CONTACT_ADDRESS_CHARS
                    and is_email_shaped(item)
                ]
            except CorruptStorageError:
                self.storage.fail_closed(
                    "Encrypted contacts could not be authenticated"
                )
                return []
            except (ValueError, RuntimeError, TypeError):
                continue
            records.append((name, phones, emails))
        return records

    def load(self) -> list[ContactRecord]:
        if self.storage is not None and not self.storage.status.can_read:
            return []
        try:
            with closing(_open_db()) as connection:
                if self.storage is None:
                    return self._plaintext_records(connection)
                discarded = self._discard_plaintext(connection)
                records = self._secure_records(connection)
                if discarded:
                    connection.execute("VACUUM")
                return records
        except sqlite3.Error as error:
            log.warning("contacts cache warm failed: %s", error)
            return []
