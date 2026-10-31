"""Toolkit-free sections and rows for the global search, shared by every client.

Input is ``Messages1.Search(query, limit)``::

    {"query", "limit",
     "conversations": {"total", "available", "results": [
         {"thread_key", "name", "is_group", "timestamp"}]},
     "messages": {"total", "available", "results": [
         {"thread_key", "handle", "thread_name", "is_group", "sender",
          "outgoing", "timestamp", "snippet", "match_start", "match_length"}]},
     "contacts": {"total", "available", "results": [ListContactCards rows]},
     "calls": {"total", "available", "results": [
         {"id", "direction", "timestamp", "address", "name", "contact_name"}]}}

Each row returned here carries a ``target`` that tells a client where the
hit lives: ``{"kind": "conversation", "thread_key"}``, ``{"kind":
"message", "thread_key", "handle"}``, ``{"kind": "contact", "contact_id"}``
(open it with GetContact) or ``{"kind": "call", "call_id"}`` (the ``id`` of
a ListCallHistory entry).

The daemon searches only what it holds decrypted in memory, so clients
should search as the user types with a short delay rather than on every
key press. Every string returned here is plain text taken verbatim from
the iPhone or from this module; each client escapes it for its own
renderer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from blueferry.contacts_view import display_name as contact_display_name
from blueferry.i18n import _, ngettext

STATUS_KEY = "search_result_limit"
MIN_QUERY_CHARS = 2
# Delay clients wait after the last key press before searching.
TYPING_DELAY_MS = 250
GROUPS = ("conversations", "messages", "contacts", "calls")


def supported(status: Mapping[str, Any]) -> bool:
    """Whether the running daemon offers Search (GetStatus capability key)."""
    value = status.get(STATUS_KEY)
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def should_search(query: str) -> bool:
    return len(" ".join(str(query).split())) >= MIN_QUERY_CHARS


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def normalize(payload: object) -> dict[str, Any]:
    """The search document with every group present and malformed rows dropped."""
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    value = payload if isinstance(payload, Mapping) else {}
    result: dict[str, Any] = {
        "query": _text(value.get("query")),
        # The per-group limit the daemon applied (it caps the requested one);
        # 0 when the reply does not say.
        "limit": _count(value.get("limit")),
    }
    for group in GROUPS:
        raw = value.get(group)
        raw = raw if isinstance(raw, Mapping) else {}
        rows = [
            item for item in raw.get("results") or []
            if isinstance(item, Mapping)
        ] if isinstance(raw.get("results"), list) else []
        result[group] = {
            "total": max(_count(raw.get("total")), len(rows)),
            "available": raw.get("available") is not False,
            "results": rows,
        }
    return result


def section_title(group: str) -> str:
    return {
        "conversations": _("Conversations"),
        "messages": _("Messages"),
        "contacts": _("Contacts"),
        "calls": _("Calls"),
    }.get(group, group)


def _time_text(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def _conversation_row(item: Mapping[str, Any]) -> dict[str, Any] | None:
    key = _text(item.get("thread_key"))
    if not key:
        return None
    return {
        "title": _text(item.get("name")) or _("Unknown conversation"),
        "subtitle": _("Group conversation") if item.get("is_group") is True else "",
        "time": _time_text(_text(item.get("timestamp"))),
        "snippet": "",
        "highlight": None,
        "target": {"kind": "conversation", "thread_key": key},
    }


def _message_row(item: Mapping[str, Any]) -> dict[str, Any] | None:
    key, handle = _text(item.get("thread_key")), _text(item.get("handle"))
    if not key or not handle:
        return None
    snippet = _text(item.get("snippet"))
    start, length = _count(item.get("match_start")), _count(item.get("match_length"))
    highlight = (start, length) if length and start + length <= len(snippet) else None
    if item.get("outgoing") is True:
        who = _("You")
    else:
        who = _text(item.get("sender"))
    return {
        "title": _text(item.get("thread_name")) or _("Unknown conversation"),
        "subtitle": who,
        "time": _time_text(_text(item.get("timestamp"))),
        "snippet": snippet,
        "highlight": highlight,
        "target": {"kind": "message", "thread_key": key, "handle": handle},
    }


def _contact_row(item: Mapping[str, Any]) -> dict[str, Any] | None:
    contact_id = _text(item.get("id"))
    if not contact_id:
        return None
    title = contact_display_name(item)
    details = [
        value for value in (_text(item.get("organization")), _text(item.get("primary")))
        if value and value != title
    ]
    return {
        "title": title,
        "subtitle": " · ".join(details),
        "time": "",
        "snippet": "",
        "highlight": None,
        "target": {"kind": "contact", "contact_id": contact_id},
    }


def _direction_text(direction: str) -> str:
    return {
        "missed": _("Missed call"),
        "incoming": _("Incoming call"),
        "outgoing": _("Outgoing call"),
    }.get(direction, _("Call"))


def _call_row(item: Mapping[str, Any]) -> dict[str, Any] | None:
    call_id = _text(item.get("id"))
    if not call_id:
        return None
    name, address = _text(item.get("name")), _text(item.get("address"))
    subtitle = [_direction_text(_text(item.get("direction")))]
    if name and address:
        subtitle.append(address)
    return {
        "title": name or address or _("Unknown caller"),
        "subtitle": " · ".join(subtitle),
        "time": _time_text(_text(item.get("timestamp"))),
        "snippet": "",
        "highlight": None,
        "target": {"kind": "call", "call_id": call_id},
    }


_ROWS = {
    "conversations": _conversation_row,
    "messages": _message_row,
    "contacts": _contact_row,
    "calls": _call_row,
}


def _more_text(total: int, shown: int) -> str:
    hidden = total - shown
    if hidden <= 0:
        return ""
    return ngettext("{count} more match", "{count} more matches", hidden).format(count=hidden)


def sections(payload: object) -> list[dict[str, Any]]:
    """The non-empty result groups in a fixed order.

    Each section: ``id`` (the group name), ``title``, ``rows``, ``total``
    and ``more`` (how many matches the limit left out, as text, or "").
    Each row: ``title``, ``subtitle``, ``time``, ``snippet`` (messages
    only), ``highlight`` (``(start, length)`` of the match in the snippet,
    or None) and ``target``.
    """
    view = normalize(payload)
    out = []
    for group in GROUPS:
        rows = [row for row in map(_ROWS[group], view[group]["results"]) if row is not None]
        if not rows:
            continue
        out.append({
            "id": group,
            "title": section_title(group),
            "rows": rows,
            "total": view[group]["total"],
            "more": _more_text(view[group]["total"], len(rows)),
        })
    return out


def hint(payload: object) -> str:
    """Why there are no (or not all) results; "" when nothing needs explaining."""
    view = normalize(payload)
    if not should_search(view["query"]):
        return _("Type at least two characters to search.")
    lines = []
    if not any(view[group]["results"] for group in GROUPS):
        lines.append(_("Nothing matches “{query}”.").format(query=view["query"]))
    if not view["calls"]["available"]:
        lines.append(_("Call history is off or locked, so calls are not searched."))
    if not view["contacts"]["available"]:
        lines.append(_("Contacts are unavailable, so they are not searched."))
    return " ".join(lines)


def limit_text(payload: object, requested: int | None = None) -> str:
    """Explain the per-group limit the daemon applied, "" when nothing is cut.

    Mentions it when a group has more matches than shown, or when the
    daemon capped a larger ``requested`` limit.
    """
    view = normalize(payload)
    limit = view["limit"]
    if not limit:
        return ""
    capped = requested is not None and requested > limit
    cut = any(view[group]["total"] > len(view[group]["results"]) for group in GROUPS)
    if not (capped or cut):
        return ""
    return ngettext(
        "Showing at most {limit} result per group.",
        "Showing at most {limit} results per group.",
        limit,
    ).format(limit=limit)


def scope_text() -> str:
    """What the search covers, for a tooltip or the empty state."""
    return _(
        "Searches recent and starred conversations, cached contacts and the "
        "retained call history on this computer."
    )


def placeholder() -> str:
    return _("Search messages, contacts and calls")


def flat_rows(payload: object) -> list[dict[str, Any]]:
    """All rows in display order with their section id, e.g. for a Quickshell list."""
    return [
        {**row, "section": section["id"], "section_title": section["title"]}
        for section in sections(payload)
        for row in section["rows"]
    ]


def target_at(payload: object, index: int) -> dict[str, Any] | None:
    """The ``target`` of row ``index`` in :func:`flat_rows`; None outside the list."""
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    rows = flat_rows(payload)
    return rows[index]["target"] if 0 <= index < len(rows) else None


def search_view(payload: object) -> dict[str, Any]:
    """The whole result view for a client."""
    groups = sections(payload)
    return {
        "sections": groups,
        "count": sum(len(section["rows"]) for section in groups),
        "hint": hint(payload),
        "limit": normalize(payload)["limit"],
        "limit_text": limit_text(payload),
    }


def search_busy_text() -> str:
    """The daemon kept answering ``NotReady`` (its search queue stayed full)."""
    return _("The search is busy. Try again in a moment.")


def search_failed_text(error: object) -> str:
    detail = str(error).strip()
    return detail or _("Search failed.")
