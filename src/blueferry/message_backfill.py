"""Fetch incoming messages that arrived while MAP was disconnected.

iOS pushes MNS events only while the phone is connected and never replays
them, so a message received while the computer was out of range or asleep
never reaches local history. After each MAP or MNS (re)connect the backend
lists the phone's inbox once and stores the messages history does not have.

Missed messages are history, not news: they are written straight to the
store, never through the event sinks, so they raise no popup, copy no code
and start no ANCS correlation. Clients learn about them from the existing
content-free ``HistoryChanged`` signal.

The listing and the history scan run on the OBEX worker. Which messages to
store is decided on the main loop, which also receives live MNS messages,
so a message that arrives both ways is stored once.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from blueferry import config
from blueferry.events import (
    SmsEvent,
    canonical_address,
    normalize_phone,
    parse_map_timestamp,
    safe_event_address,
    spelled_on_keypad,
)
from blueferry.history import (
    append_events,
    backfill_floor,
    mark_event_handles_read,
    prune_events,
    read_events,
)
from blueferry.limits import MAX_RECENT_QUERY_LIMIT
from blueferry.storage_security import NO_STORAGE, StorageSecurity

log = logging.getLogger(__name__)

# Let MNS come up and a burst of reconnect triggers settle into one listing.
BACKFILL_DELAY_SECONDS = 5
# Look back this far before the newest stored message. It absorbs clock and
# time-zone differences between the phone and the computer; duplicates in
# the overlap are removed below.
SAFETY_MARGIN = timedelta(hours=2)
# Upper bound for one listing and for the messages stored from it.
MAX_BACKFILL_MESSAGES = MAX_RECENT_QUERY_LIMIT
# A stored message without a phone timestamp is placed at its arrival time.
# Matching is one-to-one, so a wide window cannot swallow a repeated text.
DEDUP_WINDOW = timedelta(hours=3)
# The listing's Subject may be a cut-down copy of the stored body.
MIN_PREFIX_CHARS = 32
# Timestamps further ahead than this are not trusted as the newest message.
FUTURE_TOLERANCE = timedelta(days=1)
# Messages stored by the last backfills, for matching late live copies.
MAX_REMEMBERED = 2 * MAX_BACKFILL_MESSAGES

Submit = Callable[..., object]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


@dataclass(frozen=True, slots=True)
class ListedMessage:
    """One validated inbox entry from a MAP listing."""

    handle: str
    sender_address: str
    sender_alphanumeric: str | None
    body: str
    timestamp: datetime
    read: bool


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """What the worker found: messages to store and stored ones now read."""

    missing: tuple[ListedMessage, ...]
    read_handles: tuple[str, ...]
    listed: int


def _normalized_body(text: object) -> str:
    return " ".join(str(text or "").split())


def _bodies_match(listed: str, stored: str) -> bool:
    if listed == stored:
        return True
    # iOS may shorten the listing's Subject; accept a long enough prefix.
    prefix = listed.rstrip("…").rstrip(".").rstrip()
    return len(prefix) >= MIN_PREFIX_CHARS and stored.startswith(prefix)


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _stored_time(event: dict) -> datetime | None:
    """The phone's time for a stored message, else when it arrived."""
    return _parse_time(event.get("timestamp")) or _parse_time(event.get("seen_at"))


def parse_listing(
    entries: Iterable[dict[str, Any]], *, now: datetime,
) -> list[ListedMessage]:
    """Validate raw listing fields; drop entries that cannot be placed safely.

    An entry needs a reply-safe sender identity, a body and a parseable,
    not implausibly future timestamp: without them it can neither be
    deduplicated nor shown in the right conversation.
    """
    out: list[ListedMessage] = []
    for entry in entries:
        if str(entry.get("type") or "").casefold() == "email":
            continue
        address = str(entry.get("sender_address") or "").strip()
        body = str(entry.get("body") or "")
        stamp = parse_map_timestamp(str(entry.get("timestamp") or "") or None)
        handle = str(entry.get("handle") or "")
        if (
            not handle
            or not body.strip()
            or canonical_address(address) is None
            or stamp is None
            or stamp > now + FUTURE_TOLERANCE
        ):
            continue
        name = str(entry.get("sender_name") or "")
        alphanumeric = name if name and spelled_on_keypad(name, address) else None
        out.append(ListedMessage(
            handle=handle,
            sender_address=address,
            sender_alphanumeric=alphanumeric,
            body=body,
            timestamp=stamp.astimezone(timezone.utc),
            # An unknown read flag must not hide a message as already read.
            read=entry.get("read") is True,
        ))
    return out


def backfill_since(
    history: Iterable[dict],
    *,
    now: datetime,
    retention_days: int,
    floor: datetime | None = None,
) -> datetime:
    """The oldest message time worth listing.

    Messages older than retention would be pruned at once, and messages
    older than the floor were erased by the user. With received messages in
    history, listing starts a safety margin before the newest one.
    """
    lower = now - timedelta(days=retention_days)
    if floor is not None:
        lower = max(lower, floor)
    newest: datetime | None = None
    for event in history:
        if event.get("kind") != "sms_received":
            continue
        when = _stored_time(event)
        if when is None or when > now + FUTURE_TOLERANCE:
            continue
        if newest is None or when > newest:
            newest = when
    if newest is None:
        return lower
    return max(lower, newest - SAFETY_MARGIN)


def _identity(address: str | None) -> str | None:
    return canonical_address(address)


def match_listing(
    listed: Sequence[ListedMessage], stored: Sequence[dict],
) -> tuple[list[ListedMessage], list[tuple[ListedMessage, dict]]]:
    """Pair listed messages with stored ones; return (unmatched, pairs).

    A pair needs the same sender identity and matching text. The same MAP
    handle pairs first; otherwise the closest time within ``DEDUP_WINDOW``
    does. Each stored message pairs at most once, so two identical texts
    with only one of them stored leave the other unmatched.
    """
    candidates: list[tuple[float, int, int]] = []
    stored_keys: list[tuple[str | None, str, str, datetime | None]] = []
    for event in stored:
        stored_keys.append((
            _identity(safe_event_address(event)),
            _normalized_body(event.get("body")),
            str(event.get("handle") or ""),
            _stored_time(event),
        ))
    for listed_index, message in enumerate(listed):
        identity = _identity(message.sender_address)
        body = _normalized_body(message.body)
        for stored_index, (other, text, handle, when) in enumerate(stored_keys):
            if identity is None or identity != other or not _bodies_match(body, text):
                continue
            if handle and handle == message.handle:
                distance = -1.0
            elif when is None:
                continue
            else:
                distance = abs((when - message.timestamp).total_seconds())
                if distance > DEDUP_WINDOW.total_seconds():
                    continue
            candidates.append((distance, listed_index, stored_index))
    candidates.sort()
    paired_listed: set[int] = set()
    paired_stored: set[int] = set()
    pairs: list[tuple[ListedMessage, dict]] = []
    for _distance, listed_index, stored_index in candidates:
        if listed_index in paired_listed or stored_index in paired_stored:
            continue
        paired_listed.add(listed_index)
        paired_stored.add(stored_index)
        pairs.append((listed[listed_index], stored[stored_index]))
    unmatched = [
        message for index, message in enumerate(listed)
        if index not in paired_listed
    ]
    return unmatched, pairs


def plan_backfill(
    listed: Sequence[ListedMessage],
    history: Sequence[dict],
    *,
    since: datetime,
    limit: int = MAX_BACKFILL_MESSAGES,
) -> BackfillPlan:
    """Choose the newest missing messages and stored messages read on the phone."""
    window_start = since - DEDUP_WINDOW
    stored = [
        event for event in history
        if event.get("kind") == "sms_received"
        and ((when := _stored_time(event)) is None or when >= window_start)
    ]
    recent = [message for message in listed if message.timestamp >= since]
    unmatched, pairs = match_listing(recent, stored)
    unmatched.sort(key=lambda message: message.timestamp)
    read_handles = sorted({
        str(event.get("handle") or "")
        for message, event in pairs
        if message.read and event.get("is_read") is not True and event.get("handle")
    })
    kept = unmatched[-limit:] if limit > 0 else []
    return BackfillPlan(
        missing=tuple(kept),
        read_handles=tuple(read_handles),
        listed=len(listed),
    )


def history_payload(
    message: ListedMessage, *, contact_name: str | None,
) -> dict[str, Any]:
    """The stored form of one missed message.

    ``seen_at`` is the phone's time, not the time of the backfill: retention
    then counts from the message, and group correlation cannot pair it with
    an unrelated notification that happens to arrive during the backfill.
    MAP gives no group context and no notification exists for the offline
    period, so the message may have been sent to a group. It is marked
    ``group_ambiguous`` like other messages that may be from a group: the
    sender's 1:1 conversation stays read-only until the sender writes again.
    """
    event = SmsEvent(
        kind="sms_received",
        handle=message.handle,
        sender_address=message.sender_address,
        sender_phone_norm=normalize_phone(message.sender_address),
        contact_name=None if message.sender_alphanumeric else contact_name,
        body=message.body,
        timestamp=message.timestamp,
        is_read=message.read,
        sender_alphanumeric=message.sender_alphanumeric,
        seen_at=message.timestamp,
    )
    payload = event.to_dict()
    payload["group_ambiguous"] = True
    payload["backfilled"] = True
    return payload


def history_enabled(storage: StorageSecurity) -> bool:
    """Whether message history is on and currently readable and writable."""
    status = storage.status
    return status.policy != NO_STORAGE and status.can_read and status.can_write


def collect_history(
    storage: StorageSecurity,
) -> tuple[list[dict], datetime | None]:
    """Stored received messages and the erase floor, on the OBEX worker."""
    if not history_enabled(storage):
        raise RuntimeError("message history is not available")
    return read_events(kinds={"sms_received"}, storage=storage), backfill_floor()


def store_backfill(
    storage: StorageSecurity, payloads: list[dict], read_handles: list[str],
) -> bool:
    """Persist missed messages in one transaction, on the OBEX worker."""
    if not history_enabled(storage):
        raise RuntimeError("message history is not available")
    changed = False
    if payloads:
        append_events(payloads, storage=storage)
        changed = True
        # The count and size ceilings; every payload is inside retention.
        prune_events(storage=storage, expire_by_age=False)
    if read_handles and mark_event_handles_read(read_handles, storage=storage):
        changed = True
    return changed


def _live_dict(event: SmsEvent) -> dict:
    return event.to_dict()


class MessageBackfill:
    """Run one inbox backfill per MAP/MNS connection, on the OBEX worker.

    ``enabled`` is the message-history opt-in: with local storage off,
    locked or unavailable nothing is listed. ``collect`` runs on the worker
    and returns ``(history, floor)``; ``store`` runs there too and persists
    payloads and read flags, returning whether history changed.
    """

    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        session_path: Callable[[], str | None],
        submit: Submit,
        list_inbox: Callable[[str, datetime | None, int], list[dict[str, Any]]],
        collect: Callable[[], tuple[list[dict], datetime | None]],
        store: Callable[[list[dict], list[str]], bool],
        resolve_contact: Callable[[str | None], str | None],
        on_changed: Callable[[], None],
        schedule: Schedule,
        cancel: Cancel,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        retention_days: Callable[[], int] = lambda: config.HISTORY_RETENTION_DAYS,
    ) -> None:
        self._enabled = enabled
        self._session_path = session_path
        self._submit = submit
        self._list_inbox = list_inbox
        self._collect = collect
        self._store = store
        self._resolve_contact = resolve_contact
        self._on_changed = on_changed
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._now = now
        self._retention_days = retention_days
        self._timer: int | None = None
        self._busy = False
        self._rerun = False
        self._generation = 0
        # Live messages seen while a listing is in flight, as stored dicts.
        self._live: list[dict] | None = None
        # Backfilled messages, so a late live copy is not stored twice.
        self._remembered: deque[ListedMessage] = deque(maxlen=MAX_REMEMBERED)

    # -- triggers (main loop) ------------------------------------------------

    def request(self, reason: str) -> None:
        """Schedule a backfill after a MAP or MNS (re)connect."""
        if not self._enabled() or not self._session_path():
            return
        if self._busy:
            self._rerun = True
            return
        if self._timer is not None:
            return
        log.debug("missed-message backfill scheduled (%s)", reason)
        self._timer = self._schedule(BACKFILL_DELAY_SECONDS, self._fire)

    def stop(self) -> None:
        """Drop pending work when the MAP session ends or the daemon stops."""
        self._generation += 1
        self._rerun = False
        self._live = None
        self._remembered.clear()
        self._busy = False
        if self._timer is not None:
            try:
                self._cancel(self._timer)
            except Exception:
                log.debug("could not remove backfill timer", exc_info=True)
            self._timer = None

    def live_message(self, event: SmsEvent) -> bool:
        """Note a live MNS message; return whether history should store it.

        ``False`` means a backfill already stored this message: it is still
        delivered to the desktop sinks, but not written again.
        """
        if event.kind != "sms_received":
            return True
        if self._remembered:
            _unmatched, pairs = match_listing(
                list(self._remembered), [_live_dict(event)],
            )
            if pairs:
                self._remembered.remove(pairs[0][0])
                return False
        if self._live is not None:
            self._live.append(_live_dict(event))
        return True

    # -- worker round trips ---------------------------------------------------

    def _fire(self) -> bool:
        self._timer = None
        session = self._session_path()
        if not session or not self._enabled():
            return False
        generation = self._generation
        started = self._clock()
        list_inbox = self._list_inbox
        collect = self._collect
        now = self._now()
        retention = self._retention_days()

        def operation() -> tuple[BackfillPlan, datetime]:
            history, floor = collect()
            since = backfill_since(
                history, now=now, retention_days=retention, floor=floor,
            )
            listed = parse_listing(
                list_inbox(session, since, MAX_BACKFILL_MESSAGES), now=now,
            )
            return plan_backfill(listed, history, since=since), since

        self._busy = True
        self._live = []
        try:
            self._submit(
                operation,
                on_success=lambda result: self._planned(
                    generation, session, started, result[0],
                ),
                on_error=lambda error: self._failed(generation, "listing", error),
            )
        except Exception as error:
            self._failed(generation, "listing", error)
        return False

    def _planned(
        self, generation: int, session: str, started: float, plan: BackfillPlan,
    ) -> None:
        if (
            generation != self._generation
            or self._session_path() != session
            or not self._enabled()
        ):
            self._finish(generation)
            return
        live = self._live or []
        self._live = None
        missing, _pairs = match_listing(plan.missing, live)
        payloads = [
            history_payload(
                message,
                contact_name=self._resolve_contact(message.sender_address),
            )
            for message in missing
        ]
        read_handles = list(plan.read_handles)
        if not payloads and not read_handles:
            log.info(
                "missed-message backfill: nothing to add (%d listed, %.1f s)",
                plan.listed, self._clock() - started,
            )
            self._finish(generation)
            return
        self._remembered.extend(missing)
        store = self._store

        def stored(changed: bool) -> None:
            if changed:
                self._on_changed()
            log.info(
                "missed-message backfill: added %d, marked %d read "
                "(%d listed, %.1f s)",
                len(payloads), len(read_handles), plan.listed,
                self._clock() - started,
            )
            self._finish(generation)

        def failed(error: Exception) -> None:
            for message in missing:
                try:
                    self._remembered.remove(message)
                except ValueError:
                    pass
            self._failed(generation, "store", error)

        try:
            self._submit(
                lambda: store(payloads, read_handles),
                on_success=stored,
                on_error=failed,
            )
        except Exception as error:
            failed(error)

    def _failed(self, generation: int, stage: str, error: Exception) -> None:
        # Only the error class: a D-Bus message could echo phone data.
        log.warning(
            "missed-message backfill %s failed: %s", stage, type(error).__name__,
        )
        self._finish(generation)

    def _finish(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self._live = None
        if self._rerun:
            self._rerun = False
            self.request("reconnected during backfill")
