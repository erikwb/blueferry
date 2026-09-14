"""Render message text as escaped markup with clickable web URLs.

The markup is shared by GTK labels and the QML clients. Only generated anchors
are markup; message contents (including HTML) always remain literal text.
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

_WEB_URL = re.compile(
    r"(?<![\w@/])(?:https?://|www\.)[^\s<>\"\x00-\x1f\x7f]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?'\"\u2018\u2019\u201c\u201d\u2026"
_CLOSING_BRACKETS = {"(": ")", "[": "]", "{": "}"}


def _trim_url(candidate: str) -> str:
    # Keep balanced parentheses in URLs such as Wikipedia article names, while
    # leaving surrounding prose punctuation outside the clickable region.
    excess = {
        closing: candidate.count(closing) - candidate.count(opening)
        for opening, closing in _CLOSING_BRACKETS.items()
    }
    end = len(candidate)
    while end:
        last = candidate[end - 1]
        if last in _TRAILING_PUNCTUATION:
            end -= 1
        elif excess.get(last, 0) > 0:
            excess[last] -= 1
            end -= 1
        else:
            break
    return candidate[:end]


def linkify_message(body: str) -> str:
    """Return GTK-compatible link markup, preserving the original visible text.

    QML wraps this in a whitespace-preserving span and applies its theme's link
    color. The original body remains available for previews, copying and storage.
    """
    parts: list[str] = []
    offset = 0
    for match in _WEB_URL.finditer(body):
        label = _trim_url(match.group())
        url = "https://" + label if label.lower().startswith("www.") else label
        try:
            parsed = urlsplit(url)
            if not parsed.hostname or "\\" in url:
                continue
            # Accessing port also validates malformed port numbers.
            _ = parsed.port
        except ValueError:
            continue
        parts.append(escape(body[offset:match.start()]))
        parts.append(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>')
        offset = match.start() + len(label)
    parts.append(escape(body[offset:]))
    return "".join(parts)
