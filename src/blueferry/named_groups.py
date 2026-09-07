"""Named-group identity and saved reply routes, independent of event matching."""
from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable

from blueferry.events import canonical_address


def _name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def named_group_key(name: str) -> str:
    """Preserve distinct spelling; canonical Unicode forms denote the same name."""
    digest = hashlib.sha256(_name(name).encode("utf-8")).hexdigest()
    return f"group:named:v2:{digest}"


def legacy_named_group_key(name: str) -> str:
    """The old name-folding key, used only to interpret retained records."""
    normalized = " ".join(unicodedata.normalize("NFKC", name).strip().casefold().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"group:named:{digest}"


def _stored_name(event: dict) -> str:
    name = _name(str(event.get("group_name") or ""))
    key = str(event.get("group_key") or "")
    if name and key in {named_group_key(name), legacy_named_group_key(name)}:
        return name
    return ""


def stored_named_group_key(event: dict) -> str:
    """Resolve a retained key using its recorded name, without rewriting it."""
    name = _stored_name(event)
    return named_group_key(name) if name else ""


class NamedGroupRoutes:
    """Resolve legacy metadata only when one spelling owns its old key.

    The iPhone exposes no conversation ID. Even the new key cannot distinguish
    two groups with exactly the same name; it only avoids *additional* collisions
    from case folding, compatibility characters, and collapsed whitespace.
    """

    def __init__(self, events: list[dict], observed_names: Iterable[str]) -> None:
        self._names: dict[str, set[str]] = {}
        for name in (*observed_names, *(_stored_name(event) for event in events)):
            if name:
                self._names.setdefault(legacy_named_group_key(name), set()).add(_name(name))
        self._routes: dict[str, dict] = {}
        for event in events:
            if event.get("kind") != "group_route":
                continue
            name = _stored_name(event)
            if not name:
                continue
            key = named_group_key(name)
            if event["group_key"] != key and not self.aliases(name):
                # A roster saved for the previously merged thread cannot be
                # assigned to either new conversation without user review.
                continue
            raw_recipients = event.get("group_recipients")
            if not isinstance(raw_recipients, list | tuple):
                continue
            recipients = [str(value).strip() for value in raw_recipients if str(value).strip()]
            identities = [canonical_address(value) for value in recipients]
            if (
                not 2 <= len(recipients) <= 20
                or any(identity is None for identity in identities)
                or len(set(identities)) != len(identities)
            ):
                continue
            self._routes[key] = dict(event)

    def aliases(self, name: str) -> list[str]:
        legacy = legacy_named_group_key(name)
        return [legacy] if self._names.get(legacy) == {_name(name)} else []

    def annotate_stored(self, event: dict) -> None:
        """Re-key a projection copy, preserving the original history on disk."""
        kind = str(event.get("kind") or "")
        if not kind.startswith("sms_"):
            return
        name = _stored_name(event)
        if not name:
            return
        key = named_group_key(name)
        was_legacy = event["group_key"] != key
        event["group_key"] = key
        event["group_aliases"] = self.aliases(name)
        event["group_origin"] = "named"
        route = self._routes.get(key)
        if route is not None:
            # A roster edit also replaces historical outgoing routing metadata;
            # otherwise old sends could re-add a participant the user removed.
            event.update({
                "group_name": str(route.get("group_name") or ""),
                "group_members": list(route.get("group_members") or []),
                "group_recipients": list(route["group_recipients"]),
                "group_reply_ready": True,
                "group_participants_required": False,
            })
        elif was_legacy and not self.aliases(name):
            event["group_reply_ready"] = False
            event["group_participants_required"] = True

    def received(self, name: str, sender_title: str, sender_address: str | None) -> dict:
        """Apply a saved roster only when the observed sender belongs to it."""
        key = named_group_key(name)
        route = self._routes.get(key)
        recipients = [sender_address] if sender_address else []
        members = [sender_title] if sender_title else []
        reply_ready = False
        roster_changed = False
        warning_id = ""
        if route is not None:
            recipients = [str(value) for value in route["group_recipients"]]
            members = [str(value) for value in route.get("group_members", [])]
            sender_identity = canonical_address(sender_address)
            if sender_identity is not None and sender_identity in {
                canonical_address(value) for value in recipients
            }:
                reply_ready = True
            else:
                if sender_address and sender_address not in recipients:
                    recipients.append(sender_address)
                if sender_identity is not None:
                    roster_changed = True
                    route_version = str(route.get("seen_at") or key)
                    warning_id = f"{route_version}:{sender_identity}"
        return {
            "group_key": key,
            "group_aliases": self.aliases(name),
            "group_name": name,
            "group_members": members,
            "group_recipients": recipients,
            "group_reply_ready": reply_ready,
            "group_origin": "named",
            "group_participants_required": not reply_ready,
            "group_observed_recipient": sender_address or "",
            "group_observed_sender": sender_title,
            "group_roster_changed": roster_changed,
            "group_unexpected_sender": sender_title if roster_changed else "",
            "group_roster_warning_id": warning_id,
        }
