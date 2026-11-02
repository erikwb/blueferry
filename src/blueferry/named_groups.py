"""Group identity and saved reply routes, independent of event matching."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable

from blueferry.events import canonical_address

UNNAMED_GROUP_PREFIX = "group:participants:"


def _name(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def named_group_key(name: str) -> str:
    """Preserve distinct spelling; canonical Unicode forms denote the same name."""
    digest = hashlib.sha256(_name(name).encode("utf-8")).hexdigest()
    return f"group:named:v2:{digest}"


def unnamed_group_key(members: Iterable[str]) -> str:
    """Key an unnamed group by the member names iOS shows for it.

    ANCS names the other members but carries no conversation ID, so this is
    the only identity that both survives a member writing from a different
    address and is known before every address is. Renaming a member in
    Contacts or changing who is in the group changes the key. The names are
    hashed as a list, so a repeated name, or one containing a separator, still
    gives a different roster a different key.
    """
    names = json.dumps(sorted(member.casefold() for member in members))
    digest = hashlib.sha256(names.encode("utf-8")).hexdigest()
    return f"{UNNAMED_GROUP_PREFIX}v2:{digest}"


def legacy_unnamed_group_key(members: Iterable[str]) -> str:
    """The old key, which dropped repeated names and did not escape ``|``."""
    return UNNAMED_GROUP_PREFIX + "|".join(sorted({member.casefold() for member in members}))


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
    Saved rosters for unnamed groups are kept under their member-name key.
    """

    def __init__(
        self, events: list[dict], observed_names: Iterable[str],
        observed_rosters: Iterable[list[str]] = (),
    ) -> None:
        self._names: dict[str, set[str]] = {}
        for name in (*observed_names, *(_stored_name(event) for event in events)):
            if name:
                self._names.setdefault(legacy_named_group_key(name), set()).add(_name(name))
        owners: dict[str, set[str]] = {}
        for members in observed_rosters:
            names = [member.casefold() for member in members]
            exact = len(set(names)) == len(names) and not any("|" in name for name in names)
            owners.setdefault(legacy_unnamed_group_key(members), set()).add(
                unnamed_group_key(members) if exact else "",
            )
        # An old unnamed key carries over only to the one roster history shows
        # for it, and only if it spelled that roster out exactly.
        self._unnamed_successors = {
            legacy: key for legacy, (key, *others) in owners.items() if key and not others
        }
        self._unnamed_aliases = {key: legacy for legacy, key in self._unnamed_successors.items()}
        self._routes: dict[str, dict] = {}
        for event in events:
            if event.get("kind") != "group_route":
                continue
            name = _stored_name(event)
            if name:
                key = named_group_key(name)
                if event["group_key"] != key and not self.aliases(name):
                    # A roster saved for the previously merged thread cannot
                    # be assigned to either new conversation without review.
                    continue
            elif str(event.get("group_key") or "").startswith(UNNAMED_GROUP_PREFIX):
                # An old key no single roster owns keeps routing only the
                # records saved under it: no new message gets that key.
                key = self._unnamed_successors.get(event["group_key"], event["group_key"])
            else:
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

    def unnamed_aliases(self, key: str) -> list[str]:
        return [self._unnamed_aliases[key]] if key in self._unnamed_aliases else []

    def annotate_stored(self, event: dict) -> None:
        """Re-key a projection copy, preserving the original history on disk."""
        kind = str(event.get("kind") or "")
        if not kind.startswith("sms_"):
            return
        name = _stored_name(event)
        if not name:
            key = str(event.get("group_key") or "")
            if key.startswith(UNNAMED_GROUP_PREFIX):
                key = self._unnamed_successors.get(key, key)
                event["group_key"] = key
                event["group_aliases"] = self.unnamed_aliases(key)
                event["group_origin"] = "unnamed"
                if not self._apply_stored(event, key):
                    event["group_reply_ready"] = False
                    event["group_participants_required"] = True
            return
        key = named_group_key(name)
        was_legacy = event["group_key"] != key
        event["group_key"] = key
        event["group_aliases"] = self.aliases(name)
        event["group_origin"] = "named"
        if not self._apply_stored(event, key) and was_legacy and not self.aliases(name):
            event["group_reply_ready"] = False
            event["group_participants_required"] = True

    def _apply_stored(self, event: dict, key: str) -> bool:
        route = self._routes.get(key)
        if route is None:
            return False
        # A roster edit also replaces historical outgoing routing metadata;
        # otherwise old sends could re-add a participant the user removed.
        event.update({
            "group_name": str(route.get("group_name") or ""),
            "group_members": list(route.get("group_members") or []),
            "group_recipients": list(route["group_recipients"]),
            "group_reply_ready": True,
            "group_participants_required": False,
        })
        return True

    def has_route(self, key: str) -> bool:
        return key in self._routes

    def received(self, name: str, sender_title: str, sender_address: str | None) -> dict:
        """Apply a saved roster only when the observed sender belongs to it."""
        key = named_group_key(name)
        return {
            **self._received(key, sender_title, sender_address),
            "group_aliases": self.aliases(name),
            "group_name": name,
            "group_origin": "named",
        }

    def received_unnamed(
        self, key: str, name: str, sender_title: str, sender_address: str | None,
    ) -> dict:
        """Like ``received`` for an unnamed group with a saved roster."""
        return {
            **self._received(key, sender_title, sender_address),
            "group_aliases": self.unnamed_aliases(key),
            "group_name": name,
            "group_origin": "unnamed",
        }

    def _received(self, key: str, sender_title: str, sender_address: str | None) -> dict:
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
            "group_members": members,
            "group_recipients": recipients,
            "group_reply_ready": reply_ready,
            "group_participants_required": not reply_ready,
            "group_observed_recipient": sender_address or "",
            "group_observed_sender": sender_title,
            "group_roster_changed": roster_changed,
            "group_unexpected_sender": sender_title if roster_changed else "",
            "group_roster_warning_id": warning_id,
        }
