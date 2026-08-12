"""Backend-owned conversation model and reply routing metadata."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from blueferry.events import (
    canonical_address,
    display_sender_from_dict,
    safe_event_address,
)
from blueferry.grouping import correlate_group_events
from blueferry.history import history_revision
from blueferry.limits import MAX_THREAD_MESSAGES

MESSAGE_KINDS = {"sms_received", "sms_sent"}


class ConversationIndex:
    """Cache the expensive history-to-thread projection until history changes."""

    def __init__(
        self,
        loader: Callable[[], list[dict]],
        builder: Callable[[list[dict]], list[dict]],
        *,
        source: Path | None = None,
        revision: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._loader = loader
        self._builder = builder
        self._source = source
        self._revision = revision or (
            (lambda: self._file_signature(source))
            if source is not None
            else history_revision
        )
        self._signature: tuple[int, int] | None = None
        self._threads: list[dict] | None = None

    @staticmethod
    def _file_signature(source: Path) -> tuple[int, int]:
        try:
            state = source.stat()
            return state.st_mtime_ns, state.st_size
        except OSError:
            return 0, 0

    def invalidate(self) -> None:
        self._signature = None
        self._threads = None

    def threads(self) -> list[dict]:
        signature = self._revision()
        if self._threads is None or signature != self._signature:
            self._threads = self._builder(self._loader())
            self._signature = signature
        return self._threads

    def find(self, key: str) -> dict | None:
        return next((thread for thread in self.threads() if thread["key"] == key), None)


def _event_time(event: dict) -> str:
    return str(event.get("timestamp") or event.get("seen_at") or "")


def _thread_name(event: dict, address: str | None, resolver) -> str:
    if event.get("group_name"):
        return str(event["group_name"])
    if resolver is not None and address:
        resolved = resolver.resolve(address)
        if resolved:
            return str(resolved)
    return display_sender_from_dict(event)


def _message_sender(event: dict, address: str | None, resolver) -> str:
    """Return the person who sent a group message, not the group name."""
    if resolver is not None and address:
        resolved = resolver.resolve(address)
        if resolved:
            return str(resolved)
    contact = str(event.get("contact_name") or "").strip()
    if contact:
        return contact
    observed = str(event.get("group_observed_sender") or "").strip()
    if observed:
        return observed
    return address or "(unknown)"


def thread_key(event: dict) -> str | None:
    """Return a stable key that never relies on a contact display name."""
    if event.get("group_key"):
        return str(event["group_key"])
    identity = canonical_address(safe_event_address(event))
    return f"address:{identity}" if identity else None


def build_threads(events: list[dict], resolver=None) -> list[dict]:
    """Build newest-first JSON-serializable threads from event history.

    Group correlation and recipient selection happen here in the backend.
    A UI receives only an opaque thread key and calls SendToThread with it.
    """
    correlated = correlate_group_events(events, resolver)
    by_key: dict[str, dict] = {}
    for event in correlated:
        if event.get("kind") not in MESSAGE_KINDS:
            continue
        key = thread_key(event)
        if key is None:
            # An untrusted, non-address sender is visible in notification
            # history but can never become a reply target.
            continue
        is_group = bool(event.get("group_key"))
        address = safe_event_address(event)
        recipients = [str(value) for value in event.get("group_recipients", [])]
        observed_recipient = str(
            event.get("group_observed_recipient") or ""
        ).strip()
        thread = by_key.get(key)
        if thread is None:
            thread = {
                "key": key,
                "name": _thread_name(event, address, resolver),
                "is_group": is_group,
                "members": [
                    str(value) for value in event.get("group_members", [])
                ] if is_group else [],
                "recipients": recipients if is_group else ([address] if address else []),
                "observed_recipients": (
                    [observed_recipient] if observed_recipient else []
                ) if is_group else [],
                "reply_ready": (
                    bool(event.get("group_reply_ready")) if is_group
                    else address is not None
                ),
                "group_origin": str(
                    event.get("group_origin")
                    or ("named" if key.startswith("group:named:") else "")
                ),
                "participants_required": bool(
                    event.get("group_participants_required", False)
                ),
                "prompt_sender": str(
                    event.get("group_observed_sender") or ""
                ),
                "roster_changed": bool(
                    event.get("group_roster_changed", False)
                ),
                "unexpected_sender": str(
                    event.get("group_unexpected_sender") or ""
                ),
                "roster_warning_id": str(
                    event.get("group_roster_warning_id") or ""
                ),
                "messages": [],
                "last_ts": "",
            }
            by_key[key] = thread
        elif is_group:
            members = [
                str(value) for value in event.get("group_members", [])
            ]
            for member in members:
                if member not in thread["members"]:
                    thread["members"].append(member)
            for recipient in recipients:
                if recipient not in thread["recipients"]:
                    thread["recipients"].append(recipient)
                    thread["name"] = _thread_name(event, address, resolver)
            if observed_recipient and observed_recipient not in thread["observed_recipients"]:
                thread["observed_recipients"].append(observed_recipient)
            thread["reply_ready"] = (
                thread["reply_ready"] or bool(event.get("group_reply_ready"))
            )
            thread["participants_required"] = (
                thread["participants_required"]
                or bool(event.get("group_participants_required", False))
            )
            if event.get("group_origin"):
                thread["group_origin"] = str(event["group_origin"])
            if event.get("group_roster_changed"):
                thread["roster_changed"] = True
                thread["unexpected_sender"] = str(
                    event.get("group_unexpected_sender") or ""
                )
                thread["roster_warning_id"] = str(
                    event.get("group_roster_warning_id") or ""
                )

        message = {
            "handle": str(event.get("handle") or ""),
            "body": str(event.get("body") or ""),
            "timestamp": _event_time(event),
            "outgoing": event.get("kind") == "sms_sent",
            "sender": (
                "" if event.get("kind") == "sms_sent"
                else _message_sender(event, address, resolver)
            ) if is_group else "",
            "read": bool(event.get("is_read", False)),
            "body_truncated": bool(event.get("body_truncated", False)),
        }
        thread["messages"].append(message)
        if message["timestamp"] >= thread["last_ts"]:
            thread["last_ts"] = message["timestamp"]
            if event.get("group_observed_sender"):
                thread["prompt_sender"] = str(event["group_observed_sender"])

    for thread in by_key.values():
        if thread.get("participants_required"):
            thread["reply_ready"] = False
        if len(thread["messages"]) > MAX_THREAD_MESSAGES:
            thread["messages"] = thread["messages"][-MAX_THREAD_MESSAGES:]
    return sorted(by_key.values(), key=lambda item: item["last_ts"], reverse=True)


def find_thread(events: list[dict], key: str, resolver=None) -> dict | None:
    """Look up one current thread by its opaque backend key."""
    return next(
        (thread for thread in build_threads(events, resolver)
         if thread["key"] == key),
        None,
    )
