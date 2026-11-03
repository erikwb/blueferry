"""Toolkit-free logic for declining a call with a message, shared by every client.

The saved texts come from the GetStatus key ``calls_quick_replies`` (a list,
present only while calls are on and the daemon offers
``Calls1.DeclineWithMessage``). Every string returned here is plain text;
each client escapes it for its own renderer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from blueferry.i18n import _
from blueferry.models import CallInfo

STATUS_KEY = "calls_quick_replies"
# Mirrors blueferry.calls.quick_replies (the daemon validates again).
MAX_REPLIES = 5
MAX_REPLY_CHARS = 160
# A typed reply; every client caps its text field here (popups too).
MAX_CUSTOM_CHARS = 1000
# The value of the "type your own text" entry in a choice list.
CUSTOM = "\x00custom"


def supported(status: Mapping[str, Any]) -> bool:
    """Whether the daemon can decline with a message (calls on, new enough)."""
    return isinstance(status.get(STATUS_KEY), list | tuple)


def replies(status: Mapping[str, Any]) -> list[str]:
    """The saved texts; malformed entries are skipped."""
    value = status.get(STATUS_KEY)
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value[:MAX_REPLIES]:
        if isinstance(item, str) and item.strip() and item not in result:
            result.append(item)
    return result


def _field(call: CallInfo | Mapping[str, Any], name: str) -> str:
    value = getattr(call, name, None) if isinstance(call, CallInfo) else call.get(name)
    return value if isinstance(value, str) else ""


def can_decline_with_message(
    status: Mapping[str, Any], call: CallInfo | Mapping[str, Any] | None,
) -> bool:
    """Offer the action only for a ringing call whose caller has a number."""
    if call is None or not supported(status):
        return False
    return _field(call, "state") in {"incoming", "waiting"} and bool(
        _field(call, "number").strip()
    )


def button_label() -> str:
    return _("Decline with message")


def custom_label() -> str:
    return _("Own text…")


def custom_placeholder() -> str:
    return _("Message to the caller")


def dialog_title() -> str:
    return _("Decline with message")


def choices(status: Mapping[str, Any]) -> list[dict[str, str]]:
    """The menu: each saved text, then "Own text…" (value ``CUSTOM``).

    Without saved texts only "Own text…" remains.
    """
    entries = [{"value": text, "label": text} for text in replies(status)]
    entries.append({"value": CUSTOM, "label": custom_label()})
    return entries


def custom_text_error(text: str) -> str:
    """Why a typed text cannot be sent; "" when it can."""
    if not text.strip():
        return _("Type a message first.")
    if len(text.strip()) > MAX_CUSTOM_CHARS:
        return _("A message can have at most {count} characters.").format(count=MAX_CUSTOM_CHARS)
    return ""


def dialog_view(status: Mapping[str, Any]) -> dict[str, Any]:
    """Everything a decline dialog shows: title, choices, field and its limit."""
    return {
        "title": dialog_title(), "choices": choices(status), "custom": CUSTOM,
        "placeholder": custom_placeholder(), "max_chars": MAX_CUSTOM_CHARS,
    }


def sent_text() -> str:
    return _("Call declined and message sent.")


def failure_text(error: object) -> str:
    """A failed decline or send; the daemon's text says whether it declined."""
    detail = str(error).strip()
    return detail or _("Could not decline the call with a message.")


# ---- settings ---------------------------------------------------------------


def settings_label() -> str:
    return _("Replies when declining a call")


def settings_hint() -> str:
    return _(
        "Offered when you decline a ringing call with a message. "
        "Without saved replies you can still type your own text."
    )


def add_label() -> str:
    return _("Add reply")


def reply_error(text: str, existing: Sequence[str] = (), *, editing: int | None = None) -> str:
    """Why ``text`` cannot be saved as a reply; "" when it can.

    ``existing`` is the current list and ``editing`` the index being
    replaced (None when adding).
    """
    value = text.strip()
    if not value:
        return _("A reply cannot be empty.")
    if len(value) > MAX_REPLY_CHARS:
        return _("A reply can have at most {count} characters.").format(count=MAX_REPLY_CHARS)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return _("A reply must be a single line.")
    others = [item for index, item in enumerate(existing) if index != editing]
    if value in others:
        return _("This reply already exists.")
    if editing is None and len(existing) >= MAX_REPLIES:
        return _("At most {count} replies can be saved.").format(count=MAX_REPLIES)
    return ""


def with_added(existing: Sequence[str], text: str) -> list[str]:
    return [*existing, text.strip()]


def with_replaced(existing: Sequence[str], index: int, text: str) -> list[str]:
    result = list(existing)
    if 0 <= index < len(result):
        result[index] = text.strip()
    return result


def with_removed(existing: Sequence[str], index: int) -> list[str]:
    return [item for position, item in enumerate(existing) if position != index]


def can_add(existing: Sequence[str]) -> bool:
    return len(existing) < MAX_REPLIES


def settings_view(status: Mapping[str, Any]) -> dict[str, Any]:
    """The settings section: the saved replies and whether one can be added."""
    current = replies(status)
    return {
        "supported": supported(status), "label": settings_label(), "hint": settings_hint(),
        "replies": current, "can_add": can_add(current), "add_label": add_label(),
        "max_chars": MAX_REPLY_CHARS, "max_replies": MAX_REPLIES,
        "empty": _("No saved replies: declining offers only your own text."),
    }
