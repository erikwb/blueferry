"""Contacts cache — pull vCards from iPhone via PBAP, store in SQLite,
resolve phone numbers to display names.

PBAP API quirk (documented in PROTOCOL.md): use `Select(location, phonebook)`
not `SetFolder`. Then `PullAll(targetfile, filters)`.
"""
from __future__ import annotations

import logging
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING

import dbus

from blueferry import config
from blueferry.bus import obex
from blueferry.contact_photos import REJECT_TOO_LARGE, inspect_vcard_photo
from blueferry.contact_repository import ContactRecord, ContactRepository
from blueferry.events import canonical_address, is_email_shaped, normalize_phone
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_ADDRESSES_PER_CARD,
    MAX_CONTACT_NAME_CHARS,
    MAX_CONTACT_PHOTO_CHARS,
    MAX_CONTACT_PHOTO_DECODE_SECONDS,
    MAX_CONTACT_PHOTOS_TOTAL_BYTES,
    MAX_PHONEBOOK_BYTES,
    MAX_PHONEBOOK_CONTACTS,
)
from blueferry.obex.sessions import SessionManager
from blueferry.obex.transfer import wait_for_transfer
from blueferry.private_files import runtime_private_directory
from blueferry.vcard import iter_bounded_lines, iter_vcard_bodies, iter_vcard_cards

if TYPE_CHECKING:
    from blueferry.storage_security import StorageSecurity

log = logging.getLogger(__name__)

_PHONEBOOK_TRANSFER_MAX_SECONDS = 30 * 60


# ---- vCard parsing ------------------------------------------------------

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

def _parse_card(body: str) -> tuple[str | None, list[str], list[str]] | None:
    """Return one card's name with every safe phone and email address."""
    fn: str | None = None
    phones: list[str] = []
    emails: list[str] = []
    # iter_vcard_bodies already split lines at every str.splitlines()
    # boundary, so this is the same split main has always used.
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
        return (fn, list(dict.fromkeys(phones)), list(dict.fromkeys(emails)))
    return None


def _parse_vcard_records(
    blob: str | Iterable[str], *, maximum: int = MAX_PHONEBOOK_CONTACTS,
) -> list[tuple[str | None, list[str], list[str]]]:
    """Return names with every safe phone and email messaging address."""
    out: list[tuple[str | None, list[str], list[str]]] = []
    for body in iter_vcard_bodies(blob, maximum=maximum):
        record = _parse_card(body)
        if record is not None:
            out.append(record)
    return out


# Content-free size buckets for the per-sync photo summary (decoded bytes).
_PHOTO_SIZE_BUCKETS = (
    (64 * 1024, "<=64KiB"),
    (256 * 1024, "<=256KiB"),
    (1024 * 1024, "<=1MiB"),
    (4 * 1024 * 1024, "<=4MiB"),
)
PHOTO_DROPPED_SHARED = "shared"
PHOTO_DROPPED_BUDGET = "budget"
PHOTO_DROPPED_TIME = "time"


class PhotoStats:
    """Per-sync photo counters for one summary log line.

    Only counts, size buckets and rejection reasons: nothing that identifies
    a contact. Meant to show, from a real phone's journal, whether the caps
    in ``limits.py`` fit the photos iOS actually sends.
    """

    def __init__(self) -> None:
        self.sizes: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()

    def observe_size(self, size: int) -> None:
        if size <= 0:
            return
        label = next(
            (name for limit, name in _PHOTO_SIZE_BUCKETS if size <= limit),
            f">{_PHOTO_SIZE_BUCKETS[-1][1].removeprefix('<=')}",
        )
        self.sizes[label] += 1

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1

    def summary(self) -> str:
        labels = [name for _limit, name in _PHOTO_SIZE_BUCKETS]
        labels.append(f">{labels[-1].removeprefix('<=')}")
        sizes = " ".join(f"{label}:{self.sizes[label]}" for label in labels)
        dropped = " ".join(
            f"{reason}:{count}" for reason, count in sorted(self.dropped.items())
        ) or "none"
        return f"sizes {sizes}; not kept {dropped}"


def _unique_addresses(records: Sequence[ContactRecord]) -> dict[int, list[str]]:
    """Map record index to the address identities only that record owns.

    Count record ownership, not names: two people can share a name or
    number. A 10-digit and an 11-digit NANP form of one number are the same
    identity. Reply routing and photos both rely on this rule.
    """
    owners: dict[str, set[int]] = {}
    for index, (_name, phones, emails) in enumerate(records):
        for address in (*phones, *emails):
            identity = canonical_address(address)
            if identity is None:
                continue
            identities = [identity]
            if identity.startswith("phone:"):
                number = identity.removeprefix("phone:")
                if len(number) == 10:
                    identities.append(f"phone:1{number}")
                elif len(number) == 11 and number.startswith("1"):
                    identities.append(f"phone:{number[1:]}")
            for value in identities:
                owners.setdefault(value, set()).add(index)
    unique: dict[int, list[str]] = {}
    for identity, owning in owners.items():
        if len(owning) == 1:
            unique.setdefault(next(iter(owning)), []).append(identity)
    return unique


def _servable_photo_indices(records: Sequence[ContactRecord]) -> set[int]:
    """Indices of records whose photo could ever be shown.

    A record without an address of its own (every number and email is shared
    with another record) never gets its photo served, so storing it would
    only cost budget and disk.
    """
    return set(_unique_addresses([_sanitized(record) for record in records]))


def _parse_vcard_entries(
    blob: str | Iterable[str],
    *,
    maximum: int = MAX_PHONEBOOK_CONTACTS,
    clock: Callable[[], float] = time.monotonic,
    servable: Collection[int] | None = None,
    stats: PhotoStats | None = None,
) -> list[tuple[ContactRecord, bytes | None]]:
    """Return each contact record paired with its validated photo bytes.

    Used only when contact photos are enabled. Records match
    :func:`_parse_vcard_records` for the same cards; photos are decoded
    without interpreting pixels and are dropped once the per-sync photo
    budget is spent. With ``servable`` (record indices from
    :func:`_servable_photo_indices`), photos of other records are not even
    decoded and do not use budget.

    Cost on the OBEX worker: ``decode_vcard_photo`` rejects an oversized
    encoded value before any base64 work, and its JPEG header walk is capped
    in segments and fill bytes. A hostile maximum-size value measured a few
    milliseconds (base64 plus header walk), and the transfer cap (64 MiB)
    admits fewer than 50 such values, so a sync spends well under a second
    here. The byte budget only applies after decoding, so a time budget
    (spent inside photo decoding only, not in card parsing) additionally
    stops photo decoding if that estimate is ever wrong.
    """
    selected_stats = stats if stats is not None else PhotoStats()
    out: list[tuple[ContactRecord, bytes | None]] = []
    budget = MAX_CONTACT_PHOTOS_TOTAL_BYTES
    spent = 0.0

    def oversized() -> None:
        selected_stats.observe_size(MAX_CONTACT_PHOTO_CHARS * 3 // 4 + 1)
        selected_stats.drop(REJECT_TOO_LARGE)

    for body, prop in iter_vcard_cards(
        blob,
        maximum=maximum,
        max_photo_chars=MAX_CONTACT_PHOTO_CHARS,
        on_oversized_photo=oversized,
    ):
        record = _parse_card(body)
        if record is None:
            continue
        index = len(out)
        photo = None
        if prop is not None:
            if servable is not None and index not in servable:
                selected_stats.drop(PHOTO_DROPPED_SHARED)
            elif budget <= 0:
                selected_stats.drop(PHOTO_DROPPED_BUDGET)
            elif spent >= MAX_CONTACT_PHOTO_DECODE_SECONDS:
                selected_stats.drop(PHOTO_DROPPED_TIME)
            else:
                started = clock()
                photo, reason, size = inspect_vcard_photo(prop)
                spent += max(0.0, clock() - started)
                selected_stats.observe_size(size)
                if reason is not None:
                    selected_stats.drop(reason)
        if photo is not None:
            if len(photo) > budget:
                selected_stats.drop(PHOTO_DROPPED_BUDGET)
                photo = None
            else:
                budget -= len(photo)
        out.append((record, photo))
    if selected_stats.dropped[PHOTO_DROPPED_TIME]:
        log.warning(
            "contact photo decoding took too long; skipped %d photos",
            selected_stats.dropped[PHOTO_DROPPED_TIME],
        )
    if selected_stats.dropped[PHOTO_DROPPED_BUDGET]:
        log.warning(
            "contact photo budget of %d MiB reached; %d later photos were not kept",
            MAX_CONTACT_PHOTOS_TOTAL_BYTES // (1024 * 1024),
            selected_stats.dropped[PHOTO_DROPPED_BUDGET],
        )
    return out


def clear_contact_cache() -> None:
    """Erase every retained contact representation."""
    ContactRepository().clear()


# ---- PBAP pull ----------------------------------------------------------

def _phonebook_temp_root() -> Path:
    """Return an owner-only location for transient plaintext phonebooks."""
    return runtime_private_directory()


def pull_phonebook(
    sessions: SessionManager,
    *,
    max_contacts: int = 65535,
    storage: StorageSecurity | None = None,
    photos: bool | None = None,
) -> int:
    """Pull the iPhone's main phonebook over PBAP and return contact count.

    Replaces the local cache atomically (transaction). ``photos`` defaults to
    ``BLUEFERRY_CONTACT_PHOTOS``. The request is identical either way: without
    a PBAP ``Fields`` filter the iPhone already includes PHOTO in this single
    pull, so enabling photos only stops discarding bytes that were transferred
    anyway. Photos are retained only with a storage policy object.
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
        def phonebook_size() -> int:
            size = out.stat().st_size if out.exists() else 0
            if max(size, int(initial.get("Size", 0) or 0)) > MAX_PHONEBOOK_BYTES:
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
            allow_disappearance=True,
            get_progress=phonebook_size,
        )

        # obexd may remove a completed transfer object just before its output
        # becomes visible. Give the local write a bounded grace period.
        for _ in range(20):
            if out.exists() and out.stat().st_size > 0:
                break
            time.sleep(0.1)

        size = out.stat().st_size if out.exists() else 0
        log.info("transfer status: %s, file size: %d bytes", status, size)
        if size == 0:
            raise RuntimeError(
                "iPhone returned an empty phonebook; verify Settings → "
                "Bluetooth → this computer → Sync Contacts is enabled"
            )
        phonebook_size()

        if (config.CONTACT_PHOTOS if photos is None else photos) and storage is not None:
            # Two streamed reads of the same file. The first, photo-blind,
            # finds which records own an address of their own; only their
            # photos can ever be shown, so only those are decoded and counted
            # against the budget. The second yields the identical card bodies
            # plus each card's first PHOTO.
            with out.open(errors="replace") as stream:
                servable = _servable_photo_indices(_parse_vcard_records(
                    iter_bounded_lines(stream), maximum=max_contacts,
                ))
            stats = PhotoStats()
            with out.open(errors="replace") as stream:
                entries = _parse_vcard_entries(
                    iter_bounded_lines(stream),
                    maximum=max_contacts,
                    servable=servable,
                    stats=stats,
                )
            log.info(
                "parsed %d contacts (%d with photos) from %d bytes; photo %s",
                len(entries),
                sum(photo is not None for _record, photo in entries),
                size,
                stats.summary(),
            )
            return ContactRepository(storage).replace(
                [record for record, _photo in entries],
                photos=[photo for _record, photo in entries],
            )
        # Stream lines rather than read_text() plus splitlines(): the
        # phonebook may be up to MAX_PHONEBOOK_BYTES, and two whole copies of
        # it are not needed to extract bounded cards.
        with out.open(errors="replace") as stream:
            parsed = _parse_vcard_records(
                iter_bounded_lines(stream), maximum=max_contacts,
            )
        log.info("parsed %d contacts from %d bytes", len(parsed), size)

        return ContactRepository(storage).replace(parsed)


# ---- Lookup -------------------------------------------------------------

def _addresses(value: object) -> list[str]:
    """Coerce a stored address list into strings, discarding anything else.

    Records arrive from decrypted JSON, so a malformed or partially written
    row can hold a null or a bare string where a list belongs. Treat that as
    an absent field rather than letting it reach code that iterates it.
    """
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _sanitized(record: ContactRecord) -> ContactRecord:
    name, phones, emails = record
    return (
        name if isinstance(name, str) else None,
        _addresses(phones),
        _addresses(emails),
    )


class ContactsResolver:
    """In-process cache + SQLite-backed resolver. Cheap to construct.

    The instance is the stable handle held by event listeners — call
    `refresh()` to reload from disk in place, don't replace the object,
    otherwise bound `resolve` methods become stale.
    """

    def __init__(self, *, storage: StorageSecurity | None = None, strict: bool = False) -> None:
        self.storage = storage
        self._repository = ContactRepository(storage)
        self._mem: dict[str, set[str]] = {}
        self._records: list[ContactRecord] = []
        self.photo_revision = 0
        self._warm(strict=strict)

    def _warm(self, *, strict: bool = False) -> None:
        # Photo references are only loaded when the option is on, so the
        # disabled resolver is exactly the photo-blind one.
        entries = (
            self._repository.load_entries(strict=strict)
            if config.CONTACT_PHOTOS
            else [(record, None) for record in self._repository.load(strict=strict)]
        )
        loaded = [(_sanitized(record), photo) for record, photo in entries]
        # Order once here rather than per page: the cache is rebuilt only by
        # refresh(), and paging must not pay for a sort on every call.
        loaded.sort(key=lambda entry: (
            (entry[0][0] or "").casefold(), entry[0][1], entry[0][2]
        ))
        self._records.extend(record for record, _photo in loaded)
        photos = [photo for _record, photo in loaded]
        for name, phones, emails in self._records:
            if name:
                for address in (*phones, *emails):
                    self._mem.setdefault(address, set()).add(name)

        self._thread_addresses: dict[str, tuple[str, ...]] = {}
        unique = _unique_addresses(self._records)
        # Photos follow the same rule as reply routing: an address shared by
        # two records never shows either person's picture.
        self._photo_refs: dict[str, int] = {}
        for index, addresses in unique.items():
            ordered = tuple(sorted(addresses))
            for identity in ordered:
                self._thread_addresses[identity] = ordered
            photo = photos[index] if index < len(photos) else None
            if photo is not None:
                for identity in ordered:
                    self._photo_refs[identity] = photo

    def thread_addresses(self, raw: str | None) -> tuple[str, ...]:
        """Unambiguous address identities from the same PBAP record."""
        return self._thread_addresses.get(canonical_address(raw) or "", ())

    def snapshot(self) -> ContactsResolver:
        """Capture lookup state without reopening the repository on a worker."""
        resolver = copy(self)
        resolver._mem = self._mem.copy()
        resolver._records = self._records.copy()
        resolver._thread_addresses = self._thread_addresses.copy()
        resolver._photo_refs = self._photo_refs.copy()
        return resolver

    def adopt_cache(self, prepared: ContactsResolver) -> None:
        """Install a worker-built cache while keeping this resolver's live repository."""
        self._mem = prepared._mem
        self._records = prepared._records
        self._thread_addresses = prepared._thread_addresses
        self._photo_refs = prepared._photo_refs
        # Content-free: lets clients drop cached avatars after any reload.
        self.photo_revision += 1

    def refresh(self) -> int:
        """Replace the cache only after a successful read; propagate failures."""
        prepared = ContactsResolver(storage=self.storage, strict=True)
        self.adopt_cache(prepared)
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

    def records(self, offset: int = 0, limit: int | None = None) -> list[ContactRecord]:
        """Stable page of complete records — name with all of its addresses.

        `find_by_name` answers "who owns this destination"; this answers "who
        is in the phonebook", which a caller cannot reconstruct from searches.
        The cache is already ordered by display name, so paging stays stable
        between calls and costs a slice.
        """
        start = max(0, int(offset))
        if limit is None:
            return self._records[start:]
        return self._records[start:start + max(0, int(limit))]

    def resolve(self, raw: str | None) -> str | None:
        def unique_name(address: str) -> str | None:
            names = self._mem.get(address, set())
            return next(iter(names)) if len(names) == 1 else None

        value = (raw or "").strip()
        if is_email_shaped(value):
            return unique_name(value.casefold())
        norm = normalize_phone(raw)
        if not norm:
            return None
        if norm in self._mem:
            return unique_name(norm)
        # NANP numbers are commonly stored both with and without country code
        # 1. Never generalize this to suffix matching: a longer international
        # number can share its last ten digits with an unrelated NANP contact.
        alternate = (
            f"1{norm}"
            if len(norm) == 10
            else norm[1:]
            if len(norm) == 11 and norm.startswith("1")
            else ""
        )
        return unique_name(alternate) if alternate else None

    def count(self) -> int:
        return len(self._mem)

    def photo_ref(self, raw: str | None) -> int | None:
        """Stored photo id for the one record that owns ``raw``, if any."""
        identity = canonical_address(raw)
        if identity is None:
            return None
        return self._photo_refs.get(identity)

    def photo_count(self) -> int:
        return len(set(self._photo_refs.values()))

    def load_photo(self, ref: int) -> bytes | None:
        return self._repository.load_photo(ref)

    def photo(self, raw: str | None) -> bytes | None:
        """Validated photo bytes for an unambiguous address, else ``None``."""
        ref = self.photo_ref(raw)
        return self.load_photo(ref) if ref is not None else None
