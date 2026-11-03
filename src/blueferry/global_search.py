"""One search over conversations, messages, contacts and call history.

Local history is encrypted at rest, so there is no search index on disk
(no SQLite FTS, no plaintext token table). The daemon searches the data it
already holds decrypted in memory for its clients: the projected
conversation list (the most recent messages plus starred conversations),
the contact cache, and the retained call history. Nothing is precomputed
while nobody searches; every group is cut to a caller-chosen limit.

Matching is a substring test after collapsing whitespace and folding
case, accents and Unicode spelling variants (see
:mod:`blueferry.text_fold`), so ``muller`` finds ``Müller`` however the
iPhone composed it. Phone numbers additionally match by their digits once
the query has at least three digits and nothing but phone punctuation.

The folded message bodies of one conversation projection are kept in a
:class:`ThreadSearchIndex` (memory only) so a client searching while the
user types does not fold every body again on each keystroke.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from blueferry.call_history import CallRecord, call_entry_id, resolve_contact_name
from blueferry.limits import MAX_SEARCH_SNIPPET_CHARS
from blueferry.text_fold import fold, folded_with_offsets, original_span
from blueferry.threads import timestamp_order

_PHONE_QUERY = re.compile(r"[\d\s+()./-]+")
_MIN_DIGIT_QUERY = 3
_NON_DIGITS = re.compile(r"\D")


def normalize_query(query: str) -> str:
    return " ".join(str(query).split())


def _digit_needle(needle: str) -> str:
    digits = _NON_DIGITS.sub("", needle)
    if len(digits) >= _MIN_DIGIT_QUERY and _PHONE_QUERY.fullmatch(needle):
        return digits
    return ""


def snippet(text: str, start: int, length: int) -> tuple[str, int, int]:
    """A one-line excerpt around a match: ``(text, match start, match length)``.

    ``text`` is already whitespace-collapsed and ``start``/``length`` are
    the match span in it. The excerpt is at most MAX_SEARCH_SNIPPET_CHARS
    long (plus ellipses) and keeps some context before the match; the
    offsets refer to the excerpt and are clamped to it.
    """
    start = max(0, min(start, len(text)))
    before = min(start, MAX_SEARCH_SNIPPET_CHARS // 4)
    begin = start - before
    end = min(len(text), begin + MAX_SEARCH_SNIPPET_CHARS)
    excerpt = text[begin:end]
    offset = before
    if begin > 0:
        excerpt, offset = "…" + excerpt, offset + 1
    if end < len(text):
        excerpt += "…"
    return excerpt, offset, max(0, min(length, len(excerpt) - offset))


def _collapsed(text: object) -> str:
    return " ".join(str(text or "").split())


class ThreadSearchIndex:
    """Folded names, members, number digits and bodies of one projection.

    Built off the GLib loop and reused while the projection (the same
    ``threads`` list object) is current. Holds no more than the projection
    itself already holds decrypted, and never touches disk.
    """

    def __init__(self, threads: Sequence[dict]) -> None:
        self.threads = threads
        self._entries: list[tuple[dict, tuple[str, ...], str, list[tuple[dict, str]]]] = []
        for thread in threads:
            name = str(thread.get("name") or "")
            members = [str(value) for value in thread.get("members") or ()]
            recipients = [str(value) for value in thread.get("recipients") or ()]
            digits = " ".join(
                _NON_DIGITS.sub("", value) for value in (name, *members, *recipients)
            )
            bodies = [
                (message, fold(_collapsed(message.get("body"))))
                for message in thread.get("messages") or ()
            ]
            self._entries.append((
                thread, tuple(fold(value) for value in (name, *members)), digits, bodies,
            ))

    def matches(self, threads: Sequence[dict]) -> bool:
        """Whether this index was built from exactly this projection."""
        return self.threads is threads

    def search(
        self, needle: str, limit: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(conversations group, messages group)`` for one query."""
        needle = normalize_query(needle)
        folded = fold(needle)
        digits = _digit_needle(needle)
        conversations: list[dict[str, Any]] = []
        conversation_total = 0
        hits: list[tuple[float, dict, dict, int]] = []
        message_total = 0
        for thread, names, numbers, bodies in self._entries:
            if (folded and any(folded in value for value in names)) or (
                digits and digits in numbers
            ):
                conversation_total += 1
                if len(conversations) < limit:
                    conversations.append({
                        "thread_key": str(thread.get("key") or ""),
                        "name": str(thread.get("name") or ""),
                        "is_group": bool(thread.get("is_group")),
                        "timestamp": str(thread.get("last_ts") or ""),
                    })
            if not folded:
                continue
            for message, body in bodies:
                position = body.find(folded)
                if position < 0:
                    continue
                message_total += 1
                hits.append((
                    timestamp_order(str(message.get("timestamp") or "")),
                    thread, message, position,
                ))
        # Newest first, comparing instants (local offsets and UTC may differ).
        hits.sort(key=lambda item: item[0], reverse=True)
        messages = []
        for _order, thread, message, position in hits[:limit]:
            body = _collapsed(message.get("body"))
            _folded, origin = folded_with_offsets(body)
            begin, end = original_span(body, origin, position, len(folded))
            text, start, length = snippet(body, begin, end - begin)
            messages.append({
                "thread_key": str(thread.get("key") or ""),
                "handle": str(message.get("handle") or ""),
                "thread_name": str(thread.get("name") or ""),
                "is_group": bool(thread.get("is_group")),
                "sender": str(message.get("sender") or ""),
                "outgoing": bool(message.get("outgoing")),
                "timestamp": str(message.get("timestamp") or ""),
                "snippet": text,
                "match_start": start,
                "match_length": length,
            })
        return (
            {"total": conversation_total, "results": conversations},
            {"total": message_total, "results": messages},
        )


def search_threads(
    threads: Sequence[dict], needle: str, limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(conversations group, messages group)`` for one query.

    Conversations match by name or member, and by the digits of their name,
    members and recipients; messages by body text, newest first. Each
    message hit carries the thread key and the message handle a client
    needs to open it. Use a kept :class:`ThreadSearchIndex` for repeated
    queries over one projection.
    """
    return ThreadSearchIndex(threads).search(needle, limit)


def search_calls(
    records: Iterable[CallRecord],
    needle: str,
    limit: int,
    resolve: Callable[[str | None], str | None] | None,
) -> dict[str, Any]:
    """Call-history hits by caller name or number, newest first (as stored)."""
    needle = normalize_query(needle)
    folded = fold(needle)
    digits = _digit_needle(needle)
    results: list[dict[str, Any]] = []
    total = 0
    for record in records:
        resolved = resolve_contact_name(record, resolve) if resolve is not None else None
        name = resolved or record.name or ""
        number = _NON_DIGITS.sub("", record.address or record.phone or "")
        if not ((folded and name and folded in fold(name)) or (digits and digits in number)):
            continue
        total += 1
        if len(results) < limit:
            results.append({
                "id": call_entry_id(record),
                "direction": record.direction,
                "timestamp": record.occurred_at.isoformat(),
                "address": record.address,
                "name": name,
                "contact_name": resolved,
            })
    return {"total": total, "results": results}
