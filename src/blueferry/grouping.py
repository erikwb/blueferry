"""Correlate MAP iMessages with ANCS Messages notification metadata.

iOS deliberately splits group information across two transports:

* MAP carries the message body and one sender address, but no conversation ID.
* ANCS carries the same body plus a display title/subtitle.  For an unnamed
  group, iOS formats these as ``title=<sender>`` and
  ``subtitle=To you & <other participant> ...``.

This module joins those events without delaying MAP delivery.  It is pure
apart from an optional contacts resolver, so both history loading and live UI
updates can replay the same deterministic correlation.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.events import canonical_address, safe_event_address
from blueferry.named_groups import NamedGroupRoutes
from blueferry.named_groups import named_group_key as named_group_key

CORRELATION_WINDOW_SECONDS = 60

# Backend-private row markers; never persisted or exported.
HISTORY_ROW_ID_FIELD = "_blueferry_history_row_id"
CORRELATED_ANCS_ROW_IDS_FIELD = "_blueferry_correlated_ancs_row_ids"

_TO_PREFIX = re.compile(r"^To\s+", re.IGNORECASE)
_MEMBER_SEPARATOR = re.compile(r"\s*(?:,|&)\s*")


class _MessageCandidates:
    """Index repeated bodies by time; ambiguity needs at most two candidates."""

    def __init__(self, events: list[dict], indexes: list[int]) -> None:
        self.exact: dict[str, list[tuple[float, int]]] = {}
        self.prefix: dict[str, list[tuple[float, int]]] = {}
        self.maximum_index = len(events)
        for index in indexes:
            event = events[index]
            when = _seen_at(event)
            if when is None:
                continue
            body = str(event.get("body") or "")
            entry = (when.timestamp(), index)
            self.exact.setdefault(body, []).append(entry)
            if len(body) > 256:
                self.prefix.setdefault(body[:256], []).append(entry)
        for entries in (*self.exact.values(), *self.prefix.values()):
            entries.sort()

    def matching(self, body: str, when: datetime, excluded: set[int]) -> list[int]:
        instant = when.timestamp()
        matches: list[int] = []
        buckets = [self.exact.get(body, [])]
        if len(body) == 256:
            buckets.append(self.prefix.get(body, []))
        for entries in buckets:
            start = bisect_left(entries, (instant - CORRELATION_WINDOW_SECONDS, -1))
            stop = bisect_right(
                entries, (instant + CORRELATION_WINDOW_SECONDS, self.maximum_index),
            )
            for position in range(start, stop):
                index = entries[position][1]
                if index not in excluded:
                    matches.append(index)
                    if len(matches) == 2:
                        return matches
        return matches


def _seen_at(event: dict) -> datetime | None:
    raw = str(event.get("seen_at") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _unique_names(names: list[str]) -> list[str]:
    by_folded: dict[str, str] = {}
    for raw in names:
        name = raw.strip()
        if not name or name.casefold() in {"you", "me"}:
            continue
        by_folded.setdefault(name.casefold(), name)
    return [by_folded[key] for key in sorted(by_folded)]


def group_members_from_ancs(event: dict) -> list[str] | None:
    """Extract the evidence-backed unnamed-group roster from ANCS.

    A non-empty subtitle alone is not enough to call something a group; other
    apps and future Messages versions may use subtitles for unrelated context.
    For now require the exact shape observed from iOS: ``To ... & ...``.
    """
    if event.get("app_id") != MESSAGES_APP_ID:
        return None
    title = str(event.get("title") or "").strip()
    subtitle = str(event.get("subtitle") or "").strip()
    if not title or not _TO_PREFIX.match(subtitle) or "&" not in subtitle:
        return None
    remainder = _TO_PREFIX.sub("", subtitle, count=1)
    return _unique_names([title, *_MEMBER_SEPARATOR.split(remainder)])


def named_group_from_ancs(event: dict) -> str | None:
    """Return the group name carried by the named-group ANCS layout.

    Named Messages groups use the sender as the title and the group name as
    the subtitle. Unlike the unnamed ``To ... & ...`` layout, this supplies a
    display name, not a unique conversation ID or a safe reply roster.
    """
    if event.get("app_id") != MESSAGES_APP_ID:
        return None
    title = str(event.get("title") or "").strip()
    subtitle = str(event.get("subtitle") or "").strip()
    if not title or not subtitle or group_members_from_ancs(event):
        return None
    return subtitle


def _group_identity(
    members: list[str], recipients: list[str], *, reply_ready: bool,
) -> tuple[str, str]:
    if reply_ready:
        canonical = sorted(
            identity for address in recipients
            if (identity := canonical_address(address)) is not None
        )
        prefix = "group:addresses:"
    else:
        canonical = sorted(member.casefold() for member in members)
        prefix = "group:participants:"
    display = sorted(members, key=str.casefold)
    return prefix + "|".join(canonical), ", ".join(display)


def _contact_address(resolver, name: str) -> str | None:
    if canonical_address(name):
        return name.strip()
    if resolver is None:
        return None
    exact = {
        address
        for display, address in resolver.find_by_name(name)
        if display.casefold() == name.casefold()
    }
    # Picking arbitrarily among a contact's multiple numbers could add the
    # wrong person/address to a group.  Only auto-resolve an unambiguous one.
    return next(iter(exact)) if len(exact) == 1 else None


def _member_roster_token(
    member: str,
    addresses_by_name: dict[str, set[str]],
    resolver,
) -> str:
    """Prefer a proven address while retaining unresolved display identity."""
    direct = canonical_address(member)
    if direct:
        return direct
    known = addresses_by_name.get(member.casefold(), set())
    if len(known) == 1:
        resolved = canonical_address(next(iter(known)))
        if resolved:
            return resolved
    contact = _contact_address(resolver, member)
    return canonical_address(contact) or f"name:{member.casefold()}"


def _reconcile_group_identities(
    events: list[dict],
    addresses_by_name: dict[str, set[str]],
    resolver,
) -> None:
    """Collapse provisional and reply-ready views of the same group.

    One roster can initially receive a name-based key and later an
    address-based key after contacts become available. A single verified
    recipient signature is safe to project backward. Conflicting verified
    signatures are deliberately left separate rather than guessing a route.
    """
    clusters: dict[tuple[str, ...], list[dict]] = {}
    for event in events:
        if not str(event.get("kind") or "").startswith("sms_"):
            continue
        # Named groups use their iOS name spelling as the persistent lookup
        # key. Rewriting it after a roster is saved would invalidate the UI
        # thread the user just configured.
        if event.get("group_origin") == "named":
            continue
        members = _unique_names([
            str(value) for value in event.get("group_members", [])
        ])
        if len(members) < 2:
            continue
        roster = tuple(sorted(
            _member_roster_token(member, addresses_by_name, resolver)
            for member in members
        ))
        clusters.setdefault(roster, []).append(event)

    for grouped in clusters.values():
        ready_by_signature: dict[tuple[str, ...], tuple[dict, list[str]]] = {}
        for event in grouped:
            recipients: list[str] = []
            identities: set[str] = set()
            for value in event.get("group_recipients", []):
                raw = str(value)
                identity = canonical_address(raw)
                if identity and identity not in identities:
                    identities.add(identity)
                    recipients.append(raw)
            recipient_signature = tuple(sorted(identities))
            if event.get("group_reply_ready") and len(recipient_signature) >= 2:
                ready_by_signature.setdefault(
                    recipient_signature, (event, recipients)
                )
        if len(ready_by_signature) != 1:
            continue

        verified_signature, (source, recipients) = next(
            iter(ready_by_signature.items())
        )
        key = "group:addresses:" + "|".join(verified_signature)
        name = str(source.get("group_name") or "")
        for event in grouped:
            if event.get("group_sender_verified") is False:
                continue
            event["group_key"] = key
            event["group_name"] = name
            event["group_recipients"] = recipients
            event["group_reply_ready"] = True


def correlate_group_events(events: list[dict], resolver=None) -> list[dict]:
    """Return copies of events with matched MAP messages annotated.

    Added SMS keys are ``group_key``, ``group_name``, ``group_members``,
    ``group_recipients``, and ``group_reply_ready``. Minimal Messages ANCS
    records remain in the returned sequence as correlation evidence.
    """
    # Correlation annotates only top-level keys; copying each record avoids the
    # cost of recursively cloning every message body and nested value.
    out = [dict(event) for event in events]
    sms_indexes = [
        index for index, event in enumerate(out)
        if event.get("kind") == "sms_received"
    ]
    matched: set[int] = set()
    named_routes = NamedGroupRoutes(out, (
        name for event in out if (name := named_group_from_ancs(event))
    ))
    for stored in out:
        named_routes.annotate_stored(stored)
    addresses_by_name: dict[str, set[str]] = {}
    message_candidates = _MessageCandidates(out, sms_indexes)

    # Learn trustworthy contact-name/address pairs from all MAP history first;
    # a group member may not have spoken in the current group yet.
    for event in out:
        if not str(event.get("kind") or "").startswith("sms_"):
            continue
        name = str(event.get("contact_name") or "").strip()
        address = safe_event_address(event)
        if name and address:
            addresses_by_name.setdefault(name.casefold(), set()).add(address)

    for event in out:
        kind = event.get("kind")
        if kind != "ancs_notification":
            continue

        members = group_members_from_ancs(event)
        named_group = None if members else named_group_from_ancs(event)
        if not members and not named_group:
            continue
        ancs_body = str(event.get("body") or "")
        ancs_time = _seen_at(event)
        if not ancs_body or ancs_time is None:
            continue

        candidates = message_candidates.matching(ancs_body, ancs_time, matched)
        # A wrong group reply is worse than a missed correlation. Repeated
        # texts such as "ok" are common, so body + a broad time window is not
        # enough evidence when more than one MAP message is eligible.
        if len(candidates) != 1:
            continue

        sms_index = candidates[0]
        matched.add(sms_index)
        sms = out[sms_index]

        sender_title = str(event.get("title") or "").strip()
        trusted_sender_name = str(sms.get("contact_name") or "").strip()
        sender_name_verified = bool(
            sender_title
            and trusted_sender_name
            and sender_title.casefold() == trusted_sender_name.casefold()
        )
        if (sender_title and trusted_sender_name
                and sender_title.casefold() != trusted_sender_name.casefold()):
            # The notification title should name the MAP sender. A mismatch
            # means these two events are not safe to join.
            matched.remove(sms_index)
            continue
        source_row_id = event.get(HISTORY_ROW_ID_FIELD)
        if isinstance(source_row_id, int):
            sms.setdefault(CORRELATED_ANCS_ROW_IDS_FIELD, []).append(
                source_row_id
            )
        sender_address = safe_event_address(sms)
        if sender_name_verified and sender_address:
            addresses_by_name.setdefault(sender_title.casefold(), set()).add(
                sender_address
            )

        if named_group:
            sms.update(named_routes.received(named_group, sender_title, sender_address))
            continue

        if members is None:  # Defensive: named groups return above.
            continue
        recipients: list[str] = []
        unresolved = False
        for member in members:
            if sender_title and member.casefold() == sender_title.casefold():
                # ANCS display names are remote, unauthenticated metadata. Do
                # not let a claimed title redirect the live MAP sender to a
                # contact with that name.
                address = sender_address if sender_name_verified else None
            else:
                known = addresses_by_name.get(member.casefold(), set())
                if len(known) == 1:
                    address = next(iter(known))
                else:
                    address = _contact_address(resolver, member)
            if not address:
                unresolved = True
                continue
            if address not in recipients:
                recipients.append(address)

        sender_identity = canonical_address(sender_address)
        recipient_identities = {
            canonical_address(address) for address in recipients
        }
        reply_ready = (
            not unresolved
            and len(recipients) >= 2
            and sender_identity is not None
            and sender_identity in recipient_identities
        )
        key, name = _group_identity(
            members, recipients, reply_ready=reply_ready
        )

        sms.update({
            "group_key": key,
            "group_name": name,
            "group_members": members,
            "group_recipients": recipients,
            "group_reply_ready": reply_ready,
            "group_observed_sender": sender_title,
            "group_sender_verified": sender_name_verified,
        })

    # Link direct ANCS evidence only when its body/time match is unambiguous.
    for event in out:
        if event.get("kind") != "ancs_notification":
            continue
        if event.get("app_id") != MESSAGES_APP_ID:
            continue
        if group_members_from_ancs(event) or named_group_from_ancs(event):
            continue
        source_row_id = event.get(HISTORY_ROW_ID_FIELD)
        if not isinstance(source_row_id, int):
            continue
        ancs_body = str(event.get("body") or "")
        ancs_time = _seen_at(event)
        if not ancs_body or ancs_time is None:
            continue
        direct_candidates = message_candidates.matching(ancs_body, ancs_time, set())
        if len(direct_candidates) != 1:
            continue
        sms = out[direct_candidates[0]]
        sender_title = str(event.get("title") or "").strip()
        trusted_sender_name = str(sms.get("contact_name") or "").strip()
        if (
            sender_title
            and trusted_sender_name
            and sender_title.casefold() != trusted_sender_name.casefold()
        ):
            continue
        sms.setdefault(CORRELATED_ANCS_ROW_IDS_FIELD, []).append(source_row_id)

    _reconcile_group_identities(out, addresses_by_name, resolver)
    return out
