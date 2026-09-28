"""Pure parsing and merging of PBAP call-history phonebooks.

The iPhone exposes its recent calls as three PBAP phonebooks in the ``telecom``
folder: ``ich`` (received), ``och`` (dialled), and ``mch`` (missed). A fourth
combined ``cch`` phonebook exists in the specification, but it is reported to
be unreliable on iOS, so BlueFerry pulls the three directional phonebooks and
merges them here.

Each entry is a vCard carrying ``X-IRMC-CALL-DATETIME;<TYPE>:<timestamp>``
with ``TYPE`` one of ``MISSED``, ``RECEIVED``, or ``DIALED``, plus the usual
``TEL``/``N``/``FN`` properties. Everything in the phonebook is remote,
untrusted input: lengths are bounded, control characters are neutralized, and
nothing here performs I/O.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from blueferry.events import normalize_phone
from blueferry.limits import (
    MAX_CALL_HISTORY_PER_FOLDER,
    MAX_CALL_HISTORY_RECORDS,
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_NAME_CHARS,
)
from blueferry.text_safety import terminal_text
from blueferry.vcard import iter_vcard_bodies

MISSED = "missed"
INCOMING = "incoming"
OUTGOING = "outgoing"
DIRECTIONS = frozenset({MISSED, INCOMING, OUTGOING})

# PBAP phonebook name -> direction implied by the folder when a card lacks
# (or mangles) its own X-IRMC-CALL-DATETIME type parameter.
PHONEBOOKS: tuple[tuple[str, str], ...] = (
    ("ich", INCOMING),
    ("och", OUTGOING),
    ("mch", MISSED),
)

_TYPE_DIRECTIONS = {
    "MISSED": MISSED,
    "RECEIVED": INCOMING,
    "DIALED": OUTGOING,
    "DIALLED": OUTGOING,
}

# Basic (20260928T101500) and extended (2026-09-28T10:15:00) ISO 8601, with
# optional fractional seconds and an optional UTC designator or offset.
_TIMESTAMP_RE = re.compile(
    r"^(?P<year>\d{4})-?(?P<month>\d{2})-?(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):?(?P<minute>\d{2}):?(?P<second>\d{2})"
    r"(?:[.,]\d{1,9})?"
    r"(?P<zone>Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)
_MAX_TIMESTAMP_CHARS = 64
# Beyond this many new missed calls in one sync, sinks show one summary.
MAX_INDIVIDUAL_MISSED_CALL_POPUPS = 3


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One call from the iPhone's recent-calls lists."""

    direction: str
    occurred_at: datetime  # timezone-aware, UTC
    raw_time: str          # timestamp exactly as the phone sent it (bounded)
    address: str           # display form of the number, "" when withheld
    phone: str | None      # digits-only form used for contact resolution
    name: str | None       # name the phone put on the card, if any

    @property
    def key(self) -> str:
        """Stable identity across syncs, independent of the local timezone.

        The raw timestamp is used rather than its UTC conversion so a change
        of the desktop's timezone cannot make an old call look new.
        """
        return f"{self.direction}|{self.raw_time}|{self.phone or self.address}"

    @property
    def merge_key(self) -> tuple[str, str, bool]:
        # iOS may list a missed call in both mch and ich. Keep incoming and
        # missed in one class so the pair collapses; outgoing stays separate.
        return (self.raw_time, self.phone or self.address, self.direction == OUTGOING)

    def to_storage(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "occurred_at": self.occurred_at.isoformat(),
            "raw_time": self.raw_time,
            "address": self.address,
            "phone": self.phone,
            "name": self.name,
        }

    @classmethod
    def from_storage(cls, value: object) -> CallRecord | None:
        """Rebuild a record from decrypted JSON, rejecting malformed rows."""
        if not isinstance(value, dict):
            return None
        direction = value.get("direction")
        raw_time = value.get("raw_time")
        occurred = value.get("occurred_at")
        address = value.get("address")
        phone = value.get("phone")
        name = value.get("name")
        if (
            direction not in DIRECTIONS
            or not isinstance(raw_time, str)
            or not isinstance(occurred, str)
            or not isinstance(address, str)
            or not (phone is None or isinstance(phone, str))
            or not (name is None or isinstance(name, str))
        ):
            return None
        try:
            occurred_at = datetime.fromisoformat(occurred)
        except ValueError:
            return None
        if occurred_at.tzinfo is None:
            return None
        return cls(
            direction=str(direction),
            occurred_at=occurred_at.astimezone(timezone.utc),
            raw_time=raw_time[:_MAX_TIMESTAMP_CHARS],
            address=_clean(address, MAX_CONTACT_ADDRESS_CHARS),
            phone=_clean(phone, MAX_CONTACT_ADDRESS_CHARS) or None,
            name=_clean(name, MAX_CONTACT_NAME_CHARS) or None,
        )


def parse_call_timestamp(
    value: str | None, *, local_zone: tzinfo | None = None,
) -> datetime | None:
    """Parse an X-IRMC-CALL-DATETIME value into an aware UTC datetime.

    PBAP timestamps without a zone designator are the phone's local time.
    BlueFerry assumes the desktop shares that timezone; ``local_zone`` lets
    tests pin it without touching the process environment.
    """
    text = (value or "").strip()
    if not text or len(text) > _MAX_TIMESTAMP_CHARS:
        return None
    match = _TIMESTAMP_RE.fullmatch(text)
    if match is None:
        return None
    try:
        naive = datetime(
            int(match["year"]), int(match["month"]), int(match["day"]),
            int(match["hour"]), int(match["minute"]), int(match["second"]),
        )
    except ValueError:
        return None
    zone = match["zone"]
    try:
        if zone is None:
            local = (
                naive.replace(tzinfo=local_zone)
                if local_zone is not None else naive.astimezone()
            )
        elif zone.upper() == "Z":
            local = naive.replace(tzinfo=timezone.utc)
        else:
            sign = -1 if zone[0] == "-" else 1
            digits = zone[1:].replace(":", "")
            hours = int(digits[:2])
            minutes = int(digits[2:4] or 0)
            if hours > 23 or minutes > 59:
                return None
            offset = timedelta(hours=hours, minutes=minutes)
            local = naive.replace(tzinfo=timezone(sign * offset))
        return local.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _clean(value: object, limit: int) -> str:
    """Bound one remote string and make it safe to render on one line."""
    if not isinstance(value, str):
        return ""
    return terminal_text(value[:limit]).replace("\n", " ").strip()


def _unfold(body: str) -> list[str]:
    """Join RFC 2425 folded continuation lines in linear time."""
    lines: list[list[str]] = []
    for line in body.splitlines():
        if line[:1] in {" ", "\t"} and lines:
            lines[-1].append(line[1:])
        else:
            lines.append([line])
    return ["".join(parts) for parts in lines]


def _split_property(line: str) -> tuple[str, list[str], str]:
    """Return ``(NAME, [PARAMS...], value)`` for one unfolded content line."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].strip().upper()
    # vCard 3.0 allows a group prefix ("item1.TEL"); drop it.
    if "." in name:
        name = name.rsplit(".", 1)[1]
    return name, [part.strip().upper() for part in parts[1:]], value


def _direction_from_params(params: Iterable[str]) -> str | None:
    for param in params:
        _, _, candidate = param.rpartition("=")
        for token in candidate.split(","):
            direction = _TYPE_DIRECTIONS.get(token.strip())
            if direction is not None:
                return direction
    return None


def _structured_name(value: str) -> str:
    """Render ``N:Family;Given;Middle;Prefix;Suffix`` as "Given Middle Family"."""
    parts = [part.strip() for part in value.split(";")]
    family = parts[0] if parts else ""
    given = parts[1] if len(parts) > 1 else ""
    middle = parts[2] if len(parts) > 2 else ""
    return " ".join(part for part in (given, middle, family) if part)


def parse_call_history(
    blob: str,
    *,
    folder_direction: str,
    maximum: int = MAX_CALL_HISTORY_PER_FOLDER,
    local_zone: tzinfo | None = None,
) -> list[CallRecord]:
    """Parse one call-history phonebook; unusable cards are skipped.

    A card needs a parseable timestamp: without one it cannot be ordered,
    de-duplicated across syncs, or aged out by retention.
    """
    if folder_direction not in DIRECTIONS:
        raise ValueError(f"unknown call direction: {folder_direction}")
    records: list[CallRecord] = []
    for body in iter_vcard_bodies(blob, maximum=max(0, int(maximum))):
        formatted: str | None = None
        structured: str | None = None
        telephone: str | None = None
        timestamp: str | None = None
        direction: str | None = None
        for line in _unfold(body):
            if ":" not in line:
                continue
            name, params, value = _split_property(line)
            if name == "X-IRMC-CALL-DATETIME" and timestamp is None:
                timestamp = value.strip()
                direction = _direction_from_params(params)
            elif name == "FN" and formatted is None:
                formatted = value
            elif name == "N" and structured is None:
                structured = _structured_name(value)
            elif name == "TEL" and telephone is None:
                telephone = value
        occurred = parse_call_timestamp(timestamp, local_zone=local_zone)
        if occurred is None or timestamp is None:
            continue
        address = _clean(telephone, MAX_CONTACT_ADDRESS_CHARS)
        display_name = (
            _clean(formatted, MAX_CONTACT_NAME_CHARS)
            or _clean(structured, MAX_CONTACT_NAME_CHARS)
        )
        records.append(CallRecord(
            direction=direction or folder_direction,
            occurred_at=occurred,
            raw_time=timestamp[:_MAX_TIMESTAMP_CHARS],
            address=address,
            phone=normalize_phone(address),
            # iOS labels unknown callers with their own number; that is not a
            # name and must not hide a later contact-cache resolution.
            name=display_name if display_name and display_name != address else None,
        ))
    return records


_DIRECTION_PRIORITY = {MISSED: 0, INCOMING: 1, OUTGOING: 2}


def merge_call_history(
    lists: Iterable[Iterable[CallRecord]],
    *,
    maximum: int = MAX_CALL_HISTORY_RECORDS,
) -> list[CallRecord]:
    """Merge directional lists newest-first, collapsing duplicated calls.

    A call reported both as received and missed is kept as missed. Where two
    entries collapse, the one carrying a name wins over an anonymous copy.
    """
    merged: dict[tuple[str, str, bool], CallRecord] = {}
    for records in lists:
        for record in records:
            existing = merged.get(record.merge_key)
            if existing is None:
                merged[record.merge_key] = record
                continue
            preferred = min(
                (existing, record),
                key=lambda item: (_DIRECTION_PRIORITY[item.direction], item.name is None),
            )
            if preferred.name is None and (existing.name or record.name):
                preferred = CallRecord(
                    direction=preferred.direction,
                    occurred_at=preferred.occurred_at,
                    raw_time=preferred.raw_time,
                    address=preferred.address,
                    phone=preferred.phone,
                    name=existing.name or record.name,
                )
            merged[record.merge_key] = preferred
    ordered = sorted(
        merged.values(),
        key=lambda item: (item.occurred_at, item.key),
        reverse=True,
    )
    return ordered[:max(0, int(maximum))]


@dataclass(frozen=True, slots=True)
class MissedCallNotice:
    """What a desktop sink may show about one newly missed call."""

    caller: str | None   # contact name, phone-supplied name, or number
    known_contact: bool  # resolved through the local contact cache
    occurred_at: datetime


def display_caller(record: CallRecord, resolved: str | None) -> str | None:
    """Contact-cache name first, then the phone's card name, then the number."""
    return resolved or record.name or record.address or None
