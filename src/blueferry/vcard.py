"""Linear, resource-bounded extraction of vCard blocks."""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import TextIO

from blueferry.limits import MAX_VCARD_CHARS

# An unindented line made only of base64 characters, optionally padded with
# blanks. Matched in place so a candidate line is not copied.
_BASE64_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/=]+[ \t]*")
# Properties whose values are inline binary payloads (pictures, sounds,
# public keys) that contact parsing never reads.
_SKIPPED_PROPERTIES = frozenset({"photo", "logo", "sound", "key"})


# How a skipped value continues on the lines after its property line.
_FOLDED = "folded"  # only vCard 3.0/4.0 folding (leading space or tab)
_BASE64 = "base64"  # folding or unindented vCard 2.1 base64 lines
_QUOTED_PRINTABLE = "quoted-printable"  # vCard 2.1 soft line breaks
_BASE64_PARAMETERS = frozenset({"ENCODING=B", "ENCODING=BASE64", "BASE64"})
_QUOTED_PRINTABLE_PARAMETERS = frozenset({"ENCODING=QUOTED-PRINTABLE", "QUOTED-PRINTABLE"})
_DATA_BASE64_URI = re.compile(r"[ \t]*data:[^,]*;base64,", re.IGNORECASE)


def _skipped_property(line: str) -> str | None:
    """How the skipped property starting on this line continues, if it is one.

    ``None`` means the line does not start a skipped property (a grouped name
    such as ``item1.PHOTO`` counts). Otherwise the result is ``_BASE64`` for
    ``ENCODING=b``/``BASE64`` values and ``data:...;base64,`` URIs,
    ``_QUOTED_PRINTABLE`` for vCard 2.1 ``ENCODING=QUOTED-PRINTABLE`` values,
    and ``_FOLDED`` for anything else, such as a ``VALUE=uri`` link.
    """
    if line[:1] in (" ", "\t"):
        return None  # a folded continuation never starts a property
    head, separator, value = line.partition(":")
    if not separator:
        return None  # every property has a value after ":"
    name = head.split(";", 1)[0].strip().rsplit(".", 1)[-1].casefold()
    if name not in _SKIPPED_PROPERTIES:
        return None
    parameters = {part.strip().upper() for part in head.split(";")[1:]}
    if parameters & _QUOTED_PRINTABLE_PARAMETERS:
        return _QUOTED_PRINTABLE
    if parameters & _BASE64_PARAMETERS or _DATA_BASE64_URI.match(value):
        return _BASE64
    return _FOLDED


def _is_quoted_printable(line: str) -> bool:
    """Whether a property line declares a vCard 2.1 quoted-printable value."""
    head, separator, _value = line.partition(":")
    return bool(separator) and "QUOTED-PRINTABLE" in head.upper()


def _continues_skipped(line: str, previous: str, mode: str) -> bool:
    """Whether a physical line belongs to the skipped value above it.

    vCard 3.0 and 4.0 fold with one leading space or tab, which continues
    every kind of value. vCard 2.1 BASE64 values are commonly written as
    unindented base64 lines ending at a blank line, so for a base64 value an
    unindented line made only of base64 characters (``A-Z a-z 0-9 + / =``) is
    also value data. A real property line always contains ``:`` and
    therefore never matches. Other values, such as a ``VALUE=uri`` link, get
    no such allowance, so a stray colon-less line after them is kept. A
    vCard 2.1 QUOTED-PRINTABLE value continues on the next line exactly when
    the previous line ends with the soft line break ``=``.
    """
    if line[:1] in (" ", "\t"):
        return True
    if mode == _QUOTED_PRINTABLE:
        return previous.rstrip().endswith("=")
    if mode == _BASE64:
        return _BASE64_LINE.fullmatch(line) is not None
    return False


def iter_bounded_lines(stream: TextIO, *, limit: int = MAX_VCARD_CHARS) -> Iterator[str]:
    """Read a text stream line by line, splitting any line longer than ``limit``.

    A phonebook without line breaks must not become one enormous string. A
    split piece of an over-long property is either skipped media data (base64
    only) or text that overflows the card budget anyway.
    """
    return iter(lambda: stream.readline(max(1, int(limit))), "")


def iter_vcard_bodies(
    blob: str | Iterable[str],
    *,
    maximum: int,
    max_card_chars: int = MAX_VCARD_CHARS,
) -> Iterator[str]:
    """Yield complete vCard bodies without rescanning malformed prefixes.

    A nested ``BEGIN:VCARD`` restarts the pending card. This both recovers from
    malformed input and ensures a run of unterminated begin markers stays
    linear rather than making a regex retry the remainder for every marker.
    Oversized cards are discarded through their matching terminator.

    ``blob`` is either the whole text or an iterable of lines, such as a text
    file opened with universal newlines. Iterating a file keeps only the
    current line and card in memory instead of the whole phonebook plus its
    split copy.

    PHOTO, LOGO, SOUND, and KEY properties (with their continuation lines)
    are skipped and do not count against ``max_card_chars``: nothing here
    reads them, and a large contact picture must not discard the card's name
    and addresses. Skipped lines are never retained, so memory stays bounded
    by the card budget.
    """
    selected_maximum = max(0, int(maximum))
    selected_card_limit = max(0, int(max_card_chars))
    yielded = 0
    active = False
    overflowed = False
    size = 0
    lines: list[str] = []
    skipping: str | None = None
    # A kept quoted-printable value continues on the next line after a soft
    # line break, and that line must not be mistaken for a new property.
    kept_quoted_printable = False
    previous = ""

    source = (
        blob.splitlines()
        if isinstance(blob, str)
        else (line.rstrip("\r\n") for line in blob)
    )
    for line in source:
        marker = line.strip().casefold()
        if marker == "begin:vcard":
            active = True
            overflowed = False
            skipping = None
            kept_quoted_printable = False
            size = 0
            lines = []
            continue
        if marker == "end:vcard":
            if active and not overflowed:
                yield "\n".join(lines)
                yielded += 1
                if yielded >= selected_maximum:
                    return
            active = False
            overflowed = False
            skipping = None
            kept_quoted_printable = False
            size = 0
            lines = []
            continue
        if not active or overflowed:
            continue
        if skipping is not None and _continues_skipped(line, previous, skipping):
            previous = line
            continue
        if not (kept_quoted_printable and previous.rstrip().endswith("=")):
            # Not a soft-break continuation of a kept quoted-printable value.
            skipping = _skipped_property(line)
            kept_quoted_printable = skipping is None and _is_quoted_printable(line)
        previous = line
        if skipping is not None:
            continue
        size += len(line) + 1
        if size > selected_card_limit:
            overflowed = True
            lines = []
            continue
        lines.append(line)
