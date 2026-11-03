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


def is_unnamed_group_key(key: str) -> bool:
    """Whether ``key`` is a current, versioned unnamed-group key."""
    return key.startswith(UNNAMED_GROUP_PREFIX) and not is_legacy_unnamed_group_key(key)


def is_legacy_unnamed_group_key(key: str) -> bool:
    """Whether ``key`` is the unversioned unnamed-group key used before v2.

    That key joined the member names with an unescaped ``|``, so it cannot say
    which group it was saved for, and no new message is keyed by it.
    """
    if not key.startswith(UNNAMED_GROUP_PREFIX):
        return False
    rest = key[len(UNNAMED_GROUP_PREFIX):]
    return not (
        rest.startswith("v2:") and len(rest) == 67
        and all(character in "0123456789abcdef" for character in rest[3:])
    )


def unnamed_group_key_for_legacy(key: str) -> str | None:
    """The v2 key of the group an unversioned unnamed-group key most likely meant.

    Reads the key as member names joined by ``|``. The old key dropped
    repeated names and did not escape ``|``, so for some groups this is
    another group's key. Use it only where a wrong guess is harmless, such as
    a star, never for a reply roster.
    """
    if not is_legacy_unnamed_group_key(key):
        return None
    names = key[len(UNNAMED_GROUP_PREFIX):]
    return unnamed_group_key(names.split("|")) if names else None


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


def route_version(route: dict) -> str:
    """Identify one save of a roster; a roster warning names the save it is for."""
    return str(
        route.get("seen_at") or stored_named_group_key(route)
        or route.get("group_key") or ""
    )


def held_roster_warning(route: dict) -> dict:
    """The saved warning about a sender outside this roster, if any.

    It is kept with the roster because the sender's messages can leave
    history before the user reviews the members again.
    """
    warning_id = str(route.get("group_roster_warning_id") or "")
    if not warning_id.startswith(route_version(route) + ":"):
        return {}
    return {
        "group_roster_changed": True,
        "group_unexpected_sender": str(route.get("group_unexpected_sender") or ""),
        "group_roster_warning_id": warning_id,
    }


class NamedGroupRoutes:
    """Resolve legacy metadata only when one spelling owns its old key.

    The iPhone exposes no conversation ID. Even the new key cannot distinguish
    two groups with exactly the same name; it only avoids *additional* collisions
    from case folding, compatibility characters, and collapsed whitespace.
    Saved rosters for unnamed groups are kept under their member-name key.
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
            if name:
                key = named_group_key(name)
                if event["group_key"] != key and not self.aliases(name):
                    # A roster saved for the previously merged thread cannot
                    # be assigned to either new conversation without review.
                    continue
            elif is_unnamed_group_key(str(event.get("group_key") or "")):
                key = str(event["group_key"])
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

    def annotate_stored(self, event: dict) -> None:
        """Re-key a projection copy, preserving the original history on disk."""
        kind = str(event.get("kind") or "")
        if not kind.startswith("sms_"):
            return
        name = _stored_name(event)
        if not name:
            key = str(event.get("group_key") or "")
            if is_legacy_unnamed_group_key(key):
                # No roster applies under the old key, and none can be saved
                # for it: new messages from the group join its v2 key.
                event["group_origin"] = "unnamed"
                event["group_reply_ready"] = False
                event["group_participants_required"] = False
            elif is_unnamed_group_key(key):
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
        held = held_roster_warning(route)
        event.update({
            "group_name": str(route.get("group_name") or ""),
            "group_members": list(route.get("group_members") or []),
            "group_recipients": list(route["group_recipients"]),
            "group_reply_ready": not held,
            "group_participants_required": bool(held),
            **held,
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
            "group_aliases": [],
            "group_name": name,
            "group_origin": "unnamed",
        }

    def _received(self, key: str, sender_title: str, sender_address: str | None) -> dict:
        route = self._routes.get(key)
        recipients = [sender_address] if sender_address else []
        members = [sender_title] if sender_title else []
        reply_ready = False
        roster_changed = False
        unexpected_sender = ""
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
                    unexpected_sender = sender_title
                    warning_id = f"{route_version(route)}:{sender_identity}"
            held = held_roster_warning(route)
            if held and not roster_changed:
                # A member writing again does not answer an earlier warning.
                reply_ready = False
                roster_changed = True
                unexpected_sender = held["group_unexpected_sender"]
                warning_id = held["group_roster_warning_id"]
        return {
            "group_key": key,
            "group_members": members,
            "group_recipients": recipients,
            "group_reply_ready": reply_ready,
            "group_participants_required": not reply_ready,
            "group_observed_recipient": sender_address or "",
            "group_observed_sender": sender_title,
            "group_roster_changed": roster_changed,
            "group_unexpected_sender": unexpected_sender,
            "group_roster_warning_id": warning_id,
        }
