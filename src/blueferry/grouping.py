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
from datetime import datetime

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.events import canonical_address, safe_event_address

CORRELATION_WINDOW_SECONDS = 60

_TO_PREFIX = re.compile(r"^To\s+", re.IGNORECASE)
_MEMBER_SEPARATOR = re.compile(r"\s*(?:,|&)\s*")


def _seen_at(event: dict) -> datetime | None:
    raw = str(event.get("seen_at") or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_address(event: dict) -> str | None:
    return safe_event_address(event)


def _body_matches(map_body: str, ancs_body: str) -> bool:
    if map_body == ancs_body:
        return True
    # ANCS requests at most 256 characters.  A longer MAP body can therefore
    # match its exact ANCS prefix, but short partial strings never do.
    return len(ancs_body) == 256 and map_body.startswith(ancs_body)


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
    addresses_by_name: dict[str, set[str]] = {}
    sms_by_body: dict[str, list[int]] = {}
    sms_by_ancs_prefix: dict[str, list[int]] = {}
    for sms_index in sms_indexes:
        body = str(out[sms_index].get("body") or "")
        sms_by_body.setdefault(body, []).append(sms_index)
        if len(body) > 256:
            sms_by_ancs_prefix.setdefault(body[:256], []).append(sms_index)

    # Learn trustworthy contact-name/address pairs from all MAP history first;
    # a group member may not have spoken in the current group yet.
    for event in out:
        if not str(event.get("kind") or "").startswith("sms_"):
            continue
        name = str(event.get("contact_name") or "").strip()
        address = _safe_address(event)
        if name and address:
            addresses_by_name.setdefault(name.casefold(), set()).add(address)

    for event in out:
        kind = event.get("kind")
        if kind != "ancs_notification":
            continue

        members = group_members_from_ancs(event)
        if not members:
            continue
        ancs_body = str(event.get("body") or "")
        ancs_time = _seen_at(event)
        if not ancs_body or ancs_time is None:
            continue

        eligible_indexes = list(sms_by_body.get(ancs_body, ()))
        if len(ancs_body) == 256:
            eligible_indexes.extend(sms_by_ancs_prefix.get(ancs_body, ()))
        candidates: list[tuple[float, int]] = []
        for sms_index in dict.fromkeys(eligible_indexes):
            if sms_index in matched:
                continue
            sms = out[sms_index]
            sms_time = _seen_at(sms)
            if sms_time is None:
                continue
            delta = abs((ancs_time - sms_time).total_seconds())
            if delta <= CORRELATION_WINDOW_SECONDS:
                candidates.append((delta, sms_index))
        # A wrong group reply is worse than a missed correlation. Repeated
        # texts such as "ok" are common, so body + a broad time window is not
        # enough evidence when more than one MAP message is eligible.
        if len(candidates) != 1:
            continue

        _, sms_index = candidates[0]
        matched.add(sms_index)
        sms = out[sms_index]

        sender_title = str(event.get("title") or "").strip()
        trusted_sender_name = str(sms.get("contact_name") or "").strip()
        if (sender_title and trusted_sender_name
                and sender_title.casefold() != trusted_sender_name.casefold()):
            # The notification title should name the MAP sender. A mismatch
            # means these two events are not safe to join.
            matched.remove(sms_index)
            continue
        sender_address = _safe_address(sms)
        if sender_title and sender_address:
            addresses_by_name.setdefault(sender_title.casefold(), set()).add(
                sender_address
            )

        recipients: list[str] = []
        unresolved = False
        for member in members:
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

        reply_ready = not unresolved and len(recipients) >= 2
        key, name = _group_identity(
            members, recipients, reply_ready=reply_ready
        )

        sms.update({
            "group_key": key,
            "group_name": name,
            "group_members": members,
            "group_recipients": recipients,
            "group_reply_ready": reply_ready,
        })

    _reconcile_group_identities(out, addresses_by_name, resolver)
    return out
