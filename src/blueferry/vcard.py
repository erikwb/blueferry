"""Linear, resource-bounded extraction of vCard blocks."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TextIO

from blueferry.limits import MAX_VCARD_CHARS

_BASE64_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
# Properties whose values are inline binary payloads (pictures, sounds,
# public keys) that contact parsing never reads.
_SKIPPED_PROPERTIES = frozenset({"photo", "logo", "sound", "key"})


def _skipped_property(line: str) -> tuple[str, bool] | None:
    """``(name, quoted_printable)`` when a physical line starts a skipped property.

    The name is lower-case without a group prefix (``item1.PHOTO`` is
    ``photo``). ``quoted_printable`` reports a vCard 2.1
    ``ENCODING=QUOTED-PRINTABLE`` value, which continues with soft line breaks
    instead of folding.
    """
    if line[:1] in (" ", "\t"):
        return None  # a folded continuation never starts a property
    head = line.split(":", 1)[0]
    name = head.split(";", 1)[0].strip().rsplit(".", 1)[-1].casefold()
    if name not in _SKIPPED_PROPERTIES:
        return None
    parameters = {part.strip().upper() for part in head.split(";")[1:]}
    quoted_printable = bool(
        parameters & {"ENCODING=QUOTED-PRINTABLE", "QUOTED-PRINTABLE"}
    )
    return name, quoted_printable


def _continues_skipped(line: str, previous: str, quoted_printable: bool) -> bool:
    """Whether a physical line belongs to the skipped value above it.

    vCard 3.0 folds with one leading space or tab. vCard 2.1 BASE64 values are
    commonly written as unindented base64 lines ending at a blank line, so an
    unindented line made only of base64 characters (``A-Z a-z 0-9 + / =``) is
    also treated as value data. A real property line always contains ``:``
    and therefore never matches; a stray base64-only line after the value is
    consumed with it, which can only drop text no property owns. A vCard 2.1
    QUOTED-PRINTABLE value continues on the next line exactly when the
    previous line ends with the soft line break ``=``.
    """
    if line[:1] in (" ", "\t"):
        return True
    if quoted_printable:
        return previous.rstrip().endswith("=")
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= _BASE64_CHARACTERS


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
    skipping: tuple[str, bool] | None = None
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
            size = 0
            lines = []
            continue
        if not active or overflowed:
            continue
        if skipping is not None and _continues_skipped(line, previous, skipping[1]):
            previous = line
            continue
        skipping = _skipped_property(line)
        previous = line
        if skipping is not None:
            continue
        size += len(line) + 1
        if size > selected_card_limit:
            overflowed = True
            lines = []
            continue
        lines.append(line)
