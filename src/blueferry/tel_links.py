"""Turn ``tel:`` and ``callto:`` links into a number the iPhone can dial.

Desktop applications hand a clicked phone link to the client registered for
``x-scheme-handler/tel``. The link is untrusted text from a web page, a mail,
or a PDF, so it is parsed here without I/O and only ever opens a confirmation
dialog; nothing in this module dials. The result follows the daemon's own
dial rules (``calls.model.normalize_dial_number``): an optional leading ``+``
and digits. RFC 3966 parameters such as ``;ext=`` or ``;phone-context=`` are
dropped because an extension cannot be dialed reliably over HFP; the number
itself is kept. ``*`` and ``#`` are refused like everywhere else in
BlueFerry: dialed, they form service codes that reconfigure the phone.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote

from blueferry.calls.model import EMERGENCY_NUMBERS, MAX_DIAL_DIGITS
from blueferry.conversation_state import ConversationState
from blueferry.events import canonical_address

# Bounds the text accepted from the command line and the activation D-Bus
# method, before any parsing.
MAX_PHONE_LINK_CHARS = 1024

_SCHEME_RE = re.compile(r"^(?:tel|callto):(?://)?", re.IGNORECASE)
# Visual separators RFC 3966 allows, plus spaces and slashes seen in print.
_SEPARATORS_RE = re.compile(r"[ \u00a0\-.()/]")
_NUMBER_RE = re.compile(rf"^\+?[0-9]{{1,{MAX_DIAL_DIGITS}}}$")

DialProblem = Literal["", "invalid", "emergency"]


def parse_phone_link(value: object) -> str | None:
    """Return the dialable number in a phone link or a plain number, else None."""
    if not isinstance(value, str) or len(value) > MAX_PHONE_LINK_CHARS:
        return None
    text = value.strip()
    scheme = _SCHEME_RE.match(text)
    if scheme is not None:
        text = text[scheme.end():]
    elif ":" in text:
        return None  # another scheme, e.g. sms: or https:
    # Parameters and a query follow the number; neither can be dialed.
    text = re.split(r"[;?]", text, maxsplit=1)[0]
    # Decode exactly once; an encoded ";", a control character or invalid
    # UTF-8 (decoded to U+FFFD) stays invalid.
    number = _SEPARATORS_RE.sub("", unquote(text, errors="replace"))
    return number if _NUMBER_RE.fullmatch(number) else None


def is_emergency_number(number: str) -> bool:
    """Whether the daemon would refuse ``number`` as a known emergency number."""
    return number.lstrip("+") in EMERGENCY_NUMBERS


def dial_request(link: object) -> tuple[str, DialProblem]:
    """Number to confirm and why it cannot be dialed (``""`` when it can)."""
    number = parse_phone_link(link)
    if number is None:
        return "", "invalid"
    if is_emergency_number(number):
        return number, "emergency"
    return number, ""


def known_contact_name(state: ConversationState, number: str) -> str:
    """The name of a direct conversation with ``number``, if one is known.

    This reuses the conversation keys the backend derives from the contact
    cache (one key per address of a contact), so no new matching is done.
    """
    identity = canonical_address(number)
    if not identity:
        return ""
    thread = state.thread(f"address:{identity}")
    if thread is None or thread.is_group:
        return ""
    name = thread.name.strip()
    # A conversation without a contact is named after its number.
    return "" if not name or canonical_address(name) is not None else name


def phone_link_argument(arguments: list[str]) -> tuple[str, list[str]]:
    """Split a desktop ``%u`` phone link off a client's remaining arguments.

    Only ``tel:``/``callto:`` arguments are taken, so toolkit options and
    their values (``-platform wayland``) are never mistaken for a link.
    """
    link = ""
    remaining = []
    for argument in arguments:
        if not link and _SCHEME_RE.match(argument.strip()):
            link = argument
        else:
            remaining.append(argument)
    return link, remaining
