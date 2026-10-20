"""Turn ``tel:`` and ``callto:`` links into a number the iPhone can dial.

Desktop applications hand a clicked phone link to the client registered for
``x-scheme-handler/tel``. The link is untrusted text from a web page, a mail,
or a PDF, so it is parsed here without I/O and only ever opens a confirmation
dialog; nothing in this module dials. The result follows the daemon's own
dial rules (``calls.model.normalize_dial_number``): an optional leading ``+``
and digits. Links requiring a phone context, extension, subaddress, post-dial
sequence or unknown mandatory parameter are refused: dropping these could
change the destination. Unknown optional RFC 3966 parameters may be ignored.
``*`` and ``#`` are refused like everywhere else in
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

_SCHEME_RE = re.compile(r"^(tel|callto):(?://)?", re.IGNORECASE)
# RFC 3966 section 3: parameter names are ASCII, and reserved characters in
# values must be percent-encoded. Never decode names into new delimiters.
_PARAMETER_RE = re.compile(
    r"([a-zA-Z0-9-]+)(?:=(?:[a-zA-Z0-9\[\]/:&+$_.!~*'()-]|%[0-9a-fA-F]{2})+)?"
)
_UNSUPPORTED_PARAMETERS = frozenset({"phone-context", "ext", "isub", "postd"})
# Visual separators RFC 3966 allows, plus spaces and slashes seen in print.
_SEPARATORS_RE = re.compile(r"[ \u00a0\-.()/]")
# In printed "+CC (0)…" notation, the zero may be a trunk prefix or a
# significant digit (e.g. Italy). Without numbering-plan data, refuse to guess.
_AMBIGUOUS_TRUNK_RE = re.compile(r"^\+[0-9]{1,3}[ \u00a0]*\(0\)")
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
    # Preserve the common callto:number?call form, but never silently discard
    # an arbitrary query that could contain additional dialing instructions.
    if "?" in text:
        text, query = text.split("?", 1)
        if scheme is None or scheme[1].lower() != "callto" or query != "call":
            return None
    text, *parameters = text.split(";")
    seen = set()
    for parameter in parameters:
        match = _PARAMETER_RE.fullmatch(parameter)
        if match is None or scheme is None:
            return None
        name = match[1].lower()
        # A phone-context identifies a numbering plan, not a prefix we can
        # prepend (RFC 3966 section 5.1.5). Unknown m-* parameters are required
        # to be understood before dialing (section 5.4).
        if name in seen or name in _UNSUPPORTED_PARAMETERS or name.startswith("m-"):
            return None
        seen.add(name)
    # Decode exactly once; an encoded ";", a control character or invalid
    # UTF-8 (decoded to U+FFFD) stays invalid.
    decoded = unquote(text, errors="replace").strip()
    if _AMBIGUOUS_TRUNK_RE.match(decoded):
        return None
    number = _SEPARATORS_RE.sub("", decoded)
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
