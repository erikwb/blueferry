"""Render message text as escaped markup with clickable web URLs.

The markup is shared by GTK labels and the QML clients. Only generated anchors
are markup; message contents (including HTML) always remain literal text.
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import unquote, urlsplit

_WEB_URL_START = re.compile(r"(?<![\w@/])(?:https?://|www\.)", re.IGNORECASE)
_WEB_SCHEME = re.compile(r"https?://", re.IGNORECASE | re.ASCII)
# Unicode Bidi_Control characters; ordinary RTL letters and emoji are allowed.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_TRAILING_PUNCTUATION = ".,;:!?'\"\u2018\u2019\u201c\u201d\u2026"
_URL_DELIMITERS = frozenset(
    '<>"\u201c\u201d\u2018\u2019\u00ab\u00bb\u2039\u203a'
    '\u3002\uff0c\u3001\uff01\uff1f\uff1b\uff1a'
)
_BRACKETS = {
    "(": ")", "[": "]", "{": "}",
    # Full-width and CJK brackets.
    "\uff08": "\uff09", "\uff3b": "\uff3d", "\uff5b": "\uff5d",
    "\u300c": "\u300d", "\u300e": "\u300f",
}
_CLOSING_BRACKETS = frozenset(_BRACKETS.values())


def is_safe_web_url(url: str) -> bool:
    """Allow explicit web URLs without credentials or direction-control tricks."""
    if not _WEB_SCHEME.match(url):
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    if "\\" in url or _BIDI_CONTROLS.intersection(unquote(url)):
        return False
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
        # Accessing port also validates malformed port numbers.
        _ = parsed.port
    except ValueError:
        return False
    return True


def _url_end(body: str, start: int, *, single_quoted: bool) -> int:
    """Stop at prose delimiters, keeping balanced URL brackets (including IPv6)."""
    brackets: list[str] = []
    for index in range(start, len(body)):
        char = body[index]
        if (
            char.isspace() or ord(char) < 32 or ord(char) == 127
            or char in _URL_DELIMITERS or (single_quoted and char == "'")
        ):
            return index
        if char in _BRACKETS:
            brackets.append(_BRACKETS[char])
        elif char in _CLOSING_BRACKETS:
            if not brackets or brackets[-1] != char:
                return index
            brackets.pop()
    return len(body)


def linkify_message(body: str) -> str:
    """Return GTK-compatible link markup, preserving the original visible text.

    Normalize line endings for display: Qt's rich-text parser treats CRLF as
    two breaks. QML adds a whitespace-preserving span and its theme's link color.
    The caller's original body remains unchanged for previews and storage.
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    # A direction control outside the URL can also change how its label looks.
    # Keep such messages readable, but do not make any of their text clickable.
    if _BIDI_CONTROLS.intersection(body):
        return escape(body)
    parts: list[str] = []
    offset = 0
    search_from = 0
    while match := _WEB_URL_START.search(body, search_from):
        end = _url_end(
            body, match.end(),
            single_quoted=match.start() > 0 and body[match.start() - 1] == "'",
        )
        # Resume at the delimiter, so another link immediately after it is
        # still found. Each candidate is scanned only once.
        search_from = end
        label = body[match.start():end].rstrip(_TRAILING_PUNCTUATION)
        url = "https://" + label if label.lower().startswith("www.") else label
        if not is_safe_web_url(url):
            continue
        parts.append(escape(body[offset:match.start()]))
        parts.append(f'<a href="{escape(url, quote=True)}">{escape(label)}</a>')
        offset = match.start() + len(label)
    parts.append(escape(body[offset:]))
    return "".join(parts)
