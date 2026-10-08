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
from blueferry.contact_photos import valid_photo
from blueferry.events import is_email_shaped
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
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

# Opt-in contact photos. Rows are raw image bytes sealed with the storage key
# (purpose contact-photo-v1, no base64) and referenced by id from a contact
# payload. AUTOINCREMENT keeps an id from being reused by a later sync. The
# table is created only when a sync actually stores a photo, so a profile that
# never enabled the option has an unchanged schema.
# A single statement, run with execute(): executescript() would COMMIT the
# replacement transaction early and break its atomicity.
_PHOTO_TABLE = (
    "CREATE TABLE IF NOT EXISTS contact_photos ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB NOT NULL)"
)

# Versions without contact photos only know the contact tables. Their cache
# clear (and their own replacement) deletes every secure_contacts row; this
# trigger makes SQLite delete the photos with them, so a downgrade cannot
# leave photo bytes behind. Photos never outlive their contact rows: they are
# only written in the same transaction as the contacts that reference them.
_PHOTO_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS contact_photos_follow_contacts "
    "AFTER DELETE ON secure_contacts BEGIN DELETE FROM contact_photos; END"
)

_PHOTO_PURPOSE = "contact-photo-v1"
# GetContactPhoto reads on the GLib loop. Never wait there for a sync that
# holds the database; report "busy" and let the client retry instead.
_PHOTO_READ_TIMEOUT_SECONDS = 0.05
# Result codes that mean "try again later" rather than "no photo": another
# connection holds a lock, or a read-only connection found a hot journal from
# an interrupted write (SQLITE_READONLY_ROLLBACK) that only a writer can undo.
_BUSY_ERROR_NAMES = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED"})
_BUSY_ERROR_PREFIXES = ("SQLITE_BUSY_", "SQLITE_LOCKED_", "SQLITE_READONLY_")

PhotoRef = int
ContactEntry = tuple[ContactRecord, PhotoRef | None]


class PhotoStoreBusy(RuntimeError):
    """The contact database is locked by a sync; retry the photo later."""


def _is_busy_error(error: sqlite3.Error) -> bool:
    name = getattr(error, "sqlite_errorname", None)
    if isinstance(name, str) and name:
        return name in _BUSY_ERROR_NAMES or name.startswith(_BUSY_ERROR_PREFIXES)
    # Older runtimes without error names: fall back to SQLite's messages.
    message = str(error).casefold()
    return "locked" in message or "busy" in message or "readonly" in message


def _has_photo_table(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'contact_photos'"
    ).fetchone() is not None


def _open_read_only(*, timeout: float) -> sqlite3.Connection:
    """Open the existing cache without creating files or running the schema."""
    return sqlite3.connect(
        f"{config.CONTACTS_DB.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=timeout,
    )


def _stored_address_items(value: object) -> list[object] | tuple[object, ...]:
    """Return one bounded stored collection without iterating scalar values."""
    if not isinstance(value, list | tuple):
        return []
    return value[:MAX_CONTACT_ADDRESSES_PER_CARD]


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
        if _has_photo_table(connection):
            connection.execute("DELETE FROM contact_photos")

    def clear(self) -> None:
        with closing(_open_db()) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                self._delete_all(connection)
            connection.execute("VACUUM")

    def clear_photos(self) -> bool:
        """Erase retained photos only; return whether any existed.

        Contact payloads may still carry a photo id afterwards. Such a
        dangling reference resolves to no photo. The check is read-only, so a
        profile without stored photos (including one that never enabled the
        option) is not written to, and a missing database is not created.
        """
        if not config.CONTACTS_DB.exists():
            return False
        try:
            with closing(_open_read_only(timeout=5.0)) as reader:
                if not _has_photo_table(reader) or reader.execute(
                    "SELECT NOT EXISTS (SELECT 1 FROM contact_photos)"
                ).fetchone()[0]:
                    return False
        except sqlite3.Error as error:
            # E.g. a hot journal only a writer can roll back: fall through to
            # the writable path, which recovers it and then checks again.
            log.debug("read-only photo check failed: %s", error)
        with closing(_open_db()) as connection:
            if not _has_photo_table(connection) or connection.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM contact_photos)"
            ).fetchone()[0]:
                return False
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                connection.execute("DELETE FROM contact_photos")
            connection.execute("VACUUM")
        return True

    def replace(
        self,
        records: list[ContactRecord],
        *,
        photos: list[bytes | None] | None = None,
    ) -> int:
        """Atomically replace the cache; ``photos`` aligns with ``records``.

        Photos are stored only under a storage policy object, sealed with the
        same key and replaced in the same transaction as their contacts.
        """
        now = time.time()
        with closing(_open_db()) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                self._delete_all(connection)
                if self.storage is not None and photos is not None and any(
                    photo is not None for photo in photos
                ):
                    connection.execute(_PHOTO_TABLE)
                    connection.execute(_PHOTO_TRIGGER)
                for index, (name, phones, emails) in enumerate(records):
                    if not name and not phones and not emails:
                        continue
                    if self.storage is not None:
                        photo = (
                            valid_photo(photos[index])
                            if photos is not None and index < len(photos)
                            else None
                        )
                        entry: dict[str, object] = {
                            "name": name or "", "phones": phones, "emails": emails,
                        }
                        if photo is not None:
                            cursor = connection.execute(
                                "INSERT INTO contact_photos(payload) VALUES (?)",
                                (self.storage.encrypt_bytes(
                                    photo, purpose=_PHOTO_PURPOSE,
                                ),),
                            )
                            entry["photo"] = cursor.lastrowid
                        payload = json.dumps(
                            entry,
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
                    # Last chance to roll back before the cache is replaced.
                    self.storage.ensure_current()
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

    def _secure_records(self, connection: sqlite3.Connection) -> list[ContactEntry]:
        if self.storage is None or not self.storage.status.can_read:
            return []
        records: list[ContactEntry] = []
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
                raw_name = value.get("name")
                name = (
                    raw_name[:MAX_CONTACT_NAME_CHARS]
                    if isinstance(raw_name, str)
                    else ""
                )
                phones = [
                    item for item in _stored_address_items(value.get("phones"))
                    if isinstance(item, str)
                    and len(item) <= MAX_CONTACT_ADDRESS_CHARS
                ]
                emails = [
                    item.casefold()
                    for item in _stored_address_items(value.get("emails"))
                    if isinstance(item, str)
                    and len(item) <= MAX_CONTACT_ADDRESS_CHARS
                    and is_email_shaped(item)
                ]
                raw_photo = value.get("photo")
                photo = (
                    raw_photo
                    if type(raw_photo) is int and raw_photo > 0
                    else None
                )
            except CorruptStorageError:
                self.storage.fail_closed(
                    "Encrypted contacts could not be authenticated"
                )
                return []
            except (ValueError, RuntimeError, TypeError):
                continue
            records.append(((name, phones, emails), photo))
        return records

    def load(self, *, strict: bool = False) -> list[ContactRecord]:
        return [record for record, _photo in self.load_entries(strict=strict)]

    def load_entries(self, *, strict: bool = False) -> list[ContactEntry]:
        """Load records with their photo reference (``None`` without a photo)."""
        if self.storage is not None and not self.storage.status.can_read:
            return []
        try:
            with closing(_open_db()) as connection:
                if self.storage is None:
                    return [
                        (record, None)
                        for record in self._plaintext_records(connection)
                    ]
                discarded = self._discard_plaintext(connection)
                records = self._secure_records(connection)
                if any(photo is not None for _record, photo in records):
                    # clear_photos() leaves ids in sealed payloads; never
                    # advertise a photo whose row no longer exists.
                    stored = {
                        int(row[0])
                        for row in connection.execute("SELECT id FROM contact_photos")
                    } if _has_photo_table(connection) else set()
                    records = [
                        (record, photo if photo in stored else None)
                        for record, photo in records
                    ]
                if discarded:
                    connection.execute("VACUUM")
                return records
        except sqlite3.Error as error:
            if strict:
                raise
            log.warning("contacts cache warm failed: %s", error)
            return []

    def load_photo(self, ref: PhotoRef) -> bytes | None:
        """Authenticate and return one stored photo, or ``None``.

        Opens the database read-only (no schema script, no file creation)
        with a short lock timeout. Raises :class:`PhotoStoreBusy` while a sync
        holds the database so callers can retry rather than record "no
        photo". Stored bytes are re-validated (size, signature, and declared
        dimensions) so a row written by another version can never widen what
        reaches clients.
        """
        if (
            self.storage is None
            or not self.storage.status.can_read
            or type(ref) is not int
            or ref <= 0
            or not config.CONTACTS_DB.exists()
        ):
            return None
        try:
            with closing(_open_read_only(timeout=_PHOTO_READ_TIMEOUT_SECONDS)) as connection:
                if not _has_photo_table(connection):
                    return None
                row = connection.execute(
                    "SELECT payload FROM contact_photos WHERE id = ?", (ref,)
                ).fetchone()
        except sqlite3.Error as error:
            if _is_busy_error(error):
                raise PhotoStoreBusy("contact photos are being updated") from error
            log.warning("contact photo read failed: %s", error)
            return None
        if row is None or not isinstance(row[0], bytes):
            return None
        try:
            data = self.storage.decrypt_bytes(row[0], purpose=_PHOTO_PURPOSE)
        except CorruptStorageError:
            self.storage.fail_closed("Encrypted contacts could not be authenticated")
            return None
        except (ValueError, RuntimeError, TypeError):
            return None
        return valid_photo(data)
