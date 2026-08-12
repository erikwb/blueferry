"""Contacts cache — pull vCards from iPhone via PBAP, store in SQLite,
resolve phone numbers to display names.

PBAP API quirk (documented in PROTOCOL.md): use `Select(location, phonebook)`
not `SetFolder`. Then `PullAll(targetfile, filters)`.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import dbus

from blueferry.bus import obex
from blueferry.contact_repository import ContactRecord, ContactRepository
from blueferry.events import is_email_shaped, normalize_phone
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
    MAX_CONTACT_NAME_CHARS,
    MAX_PHONEBOOK_BYTES,
    MAX_PHONEBOOK_CONTACTS,
)
from blueferry.obex.sessions import SessionManager
from blueferry.obex.transfer import wait_for_transfer
from blueferry.private_files import ensure_private_directory

if TYPE_CHECKING:
    from blueferry.storage_security import StorageSecurity

log = logging.getLogger(__name__)

_PHONEBOOK_TRANSFER_MAX_SECONDS = 30 * 60


# ---- vCard parsing ------------------------------------------------------

_VCARD_BLOCK = re.compile(
    r"BEGIN:VCARD(?P<body>.*?)END:VCARD", re.DOTALL | re.IGNORECASE
)


def _pbap_pull_filters(max_contacts: int) -> dict:
    """Build filters using the PhonebookAccess1 API's exact spelling.

    PBAP uses MaxCount and lowercase vcard30. MAP's superficially similar
    ListMessages call uses MaxListCount; mixing the two makes BlueZ create a
    transfer that immediately fails and leaves a zero-byte target file.
    """
    return {
        "MaxCount": dbus.UInt16(max_contacts),
        "Format": dbus.String("vcard30"),
    }

def _parse_vcard_records(
    blob: str, *, maximum: int = MAX_PHONEBOOK_CONTACTS,
) -> list[tuple[str | None, list[str], list[str]]]:
    """Return names with every safe phone and email messaging address."""
    out: list[tuple[str | None, list[str], list[str]]] = []
    for m in _VCARD_BLOCK.finditer(blob):
        if len(out) >= maximum:
            break
        body = m.group("body")
        fn: str | None = None
        phones: list[str] = []
        emails: list[str] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.upper().startswith("FN:"):
                name = line[3:].strip()
                fn = name[:MAX_CONTACT_NAME_CHARS] or None
            elif line.upper().startswith("TEL"):
                # forms: TEL:1234, TEL;TYPE=CELL:1234, TEL;TYPE=CELL,VOICE:1234
                _, _, val = line.partition(":")
                if len(val) > MAX_CONTACT_ADDRESS_CHARS:
                    continue
                norm = normalize_phone(val)
                if norm and len(phones) < MAX_CONTACT_ADDRESSES_PER_CARD:
                    phones.append(norm)
            elif line.upper().startswith("EMAIL"):
                _, _, value = line.partition(":")
                value = value.strip()
                if len(value) > MAX_CONTACT_ADDRESS_CHARS:
                    continue
                if (
                    is_email_shaped(value)
                    and len(emails) < MAX_CONTACT_ADDRESSES_PER_CARD
                ):
                    emails.append(value.casefold())
        if fn or phones or emails:
            out.append((fn, list(dict.fromkeys(phones)),
                        list(dict.fromkeys(emails))))
    return out


def clear_contact_cache() -> None:
    """Erase every retained contact representation."""
    ContactRepository().clear()


# ---- PBAP pull ----------------------------------------------------------

def _phonebook_temp_root() -> Path:
    """Return an owner-only location for transient plaintext phonebooks."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError(
            "contact sync requires XDG_RUNTIME_DIR so its plaintext "
            "phonebook cannot be written to persistent storage"
        )
    root = Path(runtime_dir) / "blueferry"
    ensure_private_directory(root)
    return root


def pull_phonebook(
    sessions: SessionManager,
    *,
    max_contacts: int = 65535,
    storage: StorageSecurity | None = None,
) -> int:
    """Pull the iPhone's main phonebook over PBAP and return contact count.

    Replaces the local cache atomically (transaction).
    """
    max_contacts = max(1, min(int(max_contacts), MAX_PHONEBOOK_CONTACTS))
    if storage is not None and not storage.status.can_write:
        raise RuntimeError(storage.status.detail)
    temporary_root = _phonebook_temp_root()
    pbap = obex(sessions.pbap_path, "org.bluez.obex.PhonebookAccess1")
    log.info("PBAP Select(int, pb)")
    pbap.Select("int", "pb", timeout=10.0)

    with tempfile.TemporaryDirectory(
        prefix="phonebook-", dir=temporary_root
    ) as temporary_name:
        out = Path(temporary_name) / "pb.vcf"
        log.info("PBAP PullAll → %s (max=%d)", out, max_contacts)
        ret = pbap.PullAll(
            str(out), _pbap_pull_filters(max_contacts), timeout=30.0
        )
        if isinstance(ret, tuple | list):
            transfer_path = str(ret[0])
            initial = dict(ret[1]) if len(ret) > 1 else {}
        else:
            transfer_path = str(ret)
            initial = {}

        # PullAll returns initial Transfer1 properties alongside the path.
        # Keep them: small transfers can finish and disappear before our first
        # Properties.Get call.
        initial_status = str(initial.get("Status", "queued"))
        log.debug("PBAP transfer %s initial status=%s size=%s",
                  transfer_path, initial_status, initial.get("Size", "unknown"))
        initial_size = int(initial.get("Size", 0) or 0)
        if initial_size > MAX_PHONEBOOK_BYTES:
            raise RuntimeError(
                f"phonebook exceeds {MAX_PHONEBOOK_BYTES} byte safety limit"
            )
        def phonebook_size() -> int:
            size = out.stat().st_size if out.exists() else 0
            if size > MAX_PHONEBOOK_BYTES:
                raise RuntimeError(
                    f"phonebook exceeds {MAX_PHONEBOOK_BYTES} byte safety limit"
                )
            return size

        status = wait_for_transfer(
            transfer_path,
            initial_status=initial_status,
            timeout_s=60,
            overall_timeout_s=_PHONEBOOK_TRANSFER_MAX_SECONDS,
            property_timeout_s=10.0,
            get_progress=phonebook_size,
        )

        # obexd may remove a completed transfer object just before its output
        # becomes visible. Give the local write a bounded grace period.
        if status in ("complete", "gone"):
            for _ in range(20):
                if out.exists() and out.stat().st_size > 0:
                    break
                time.sleep(0.1)

        size = out.stat().st_size if out.exists() else 0
        log.info("transfer status: %s, file size: %d bytes", status, size)
        if status == "error":
            raise RuntimeError("PBAP transfer failed")
        if size == 0:
            raise RuntimeError(
                "iPhone returned an empty phonebook; verify Settings → "
                "Bluetooth → this computer → Sync Contacts is enabled"
            )
        phonebook_size()

        blob = out.read_text(errors="replace")
        parsed = _parse_vcard_records(blob, maximum=max_contacts)
        log.info("parsed %d contacts from %d bytes", len(parsed), size)

        return ContactRepository(storage).replace(parsed)


# ---- Lookup -------------------------------------------------------------

class ContactsResolver:
    """In-process cache + SQLite-backed resolver. Cheap to construct.

    The instance is the stable handle held by event listeners — call
    `refresh()` to reload from disk in place, don't replace the object,
    otherwise bound `resolve` methods become stale.
    """

    def __init__(self, *, storage: StorageSecurity | None = None) -> None:
        self.storage = storage
        self._repository = ContactRepository(storage)
        self._mem: dict[str, str] = {}
        self._records: list[ContactRecord] = []
        self._warm()

    def _warm(self) -> None:
        self._records.extend(self._repository.load())
        for name, phones, emails in self._records:
            if name:
                for address in (*phones, *emails):
                    self._mem[address] = name

    def refresh(self) -> int:
        """Re-read the SQLite cache into memory. Returns new count."""
        self._mem.clear()
        self._records.clear()
        self._warm()
        return len(self._mem)

    def find_by_name(self, query: str) -> list[tuple[str, str]]:
        """Reverse lookup — name substring → (display_name, destination).

        Case-insensitive substring match against full_name. Returns all
        phones and emails for each matching contact, deduplicated. Empty if
        no matches.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        seen = {
            (name, address)
            for name, phones, emails in self._records
            if name and q in name.casefold()
            for address in (*phones, *emails)
        }
        return sorted(seen, key=lambda item: (item[0].casefold(), item[1]))

    def resolve(self, raw: str | None) -> str | None:
        value = (raw or "").strip()
        if is_email_shaped(value):
            return self._mem.get(value.casefold())
        norm = normalize_phone(raw)
        if not norm:
            return None
        # Match exact, or suffix-match (US numbers might be stored 10 vs 11 digit
        # depending on whether the country code +1 was included). Match in BOTH
        # directions: a 10-digit incoming might match an 11-digit stored, and
        # vice versa.
        if norm in self._mem:
            return self._mem[norm]
        if len(norm) >= 10:
            tail = norm[-10:]
            for k, v in self._mem.items():
                if k.endswith(tail):
                    return v
        return None

    def count(self) -> int:
        return len(self._mem)
