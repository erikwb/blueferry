"""Linear, resource-bounded extraction of vCard blocks."""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import TextIO

from blueferry.limits import MAX_VCARD_CHARS

# Properties whose values are inline binary payloads (pictures, sounds,
# public keys) that contact parsing never reads.
_SKIPPED_PROPERTIES = frozenset({"PHOTO", "LOGO", "SOUND", "KEY"})
_BASE64_PARAMETERS = frozenset({"ENCODING=B", "ENCODING=BASE64", "BASE64"})
_QUOTED_PRINTABLE_PARAMETERS = frozenset({"ENCODING=QUOTED-PRINTABLE", "QUOTED-PRINTABLE"})
# An unindented vCard 2.1 BASE64 continuation line, optionally padded.
_BASE64_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/=]+[ \t]*")
# A property head (``group.NAME;params``) longer than this without its ":"
# is not a property this module skips; the line is kept and budgeted.
_MAX_HEAD_CHARS = 1024


def iter_bounded_lines(stream: TextIO, *, limit: int = MAX_VCARD_CHARS) -> Iterator[str]:
    """Read a text stream line by line, splitting any line longer than ``limit``.

    A phonebook without line breaks must not become one enormous string. Only
    the last piece of a split line keeps its line break, which is how
    ``iter_vcard_bodies`` tells the pieces of one over-long line apart from
    separate lines.
    """
    return iter(lambda: stream.readline(max(1, int(limit))), "")


# A cut line is only yielded in pieces once its first piece has this many
# characters, so a marker or a property name and its parameters are never cut.
_WHOLE_LINE_START = 256


def _line_pieces(blob: str | Iterable[str]) -> Iterator[tuple[str, bool, bool]]:
    """Yield ``(text, continued, complete)`` for every piece of every line.

    Lines are split at every ``str.splitlines()`` boundary, not only at
    ``"\n"`` and ``"\r"``: the name a card yields must not depend on whether
    it came from a MAP bMessage string or from a streamed phonebook.
    ``continued`` marks a piece of a line whose start was already yielded,
    because a bounded reader cut the line. ``complete`` marks the piece that
    ends its line. A ``"\r\n"`` cut between two pieces is one line break.
    """
    chunks: Iterable[str] = (blob,) if isinstance(blob, str) else blob
    pending: tuple[str, bool, bool] | None = None
    continued = False
    after_carriage_return = False
    carry = ""
    for chunk in chunks:
        if after_carriage_return and chunk[:1] == "\n":
            chunk = chunk[1:]
        after_carriage_return = chunk[-1:] == "\r"
        if carry:
            chunk, carry = carry + chunk, ""
        for piece in chunk.splitlines(keepends=True):
            text = piece.splitlines()[0]
            terminated = len(text) < len(piece)
            if pending is not None:
                yield pending
            pending = (text, continued, terminated)
            continued = not terminated
        if (
            pending is not None
            and not pending[1]
            and not pending[2]
            and len(pending[0]) < _WHOLE_LINE_START
        ):
            # A reader cut a line shortly after its start, for example at a
            # "\r" or U+2028 it does not split at. Keep the start for the next
            # chunk so markers and property names are always seen whole.
            carry, pending, continued = pending[0], None, False
    if carry:
        yield carry, False, True
    elif pending is not None:
        yield pending[0], pending[1], True



def _split_unquoted(head: str, separator: str) -> list[str]:
    """Split a property head at ``separator`` outside double quotes."""
    parts: list[str] = []
    start = 0
    quoted = False
    for index, char in enumerate(head):
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            parts.append(head[start:index])
            start = index + 1
    parts.append(head[start:])
    return parts


class _Card:
    """One vCard, assembled from physical lines into logical properties.

    Lines are unfolded before anything is decided about them: a leading space
    or tab continues the previous line (RFC 6350/2426 remove it, vCard 2.1
    keeps it), a vCard 2.1 QUOTED-PRINTABLE value continues after a line
    ending in the soft line break ``=``, and a vCard 2.1 BASE64 value
    continues on unindented base64 lines up to a blank line. Each logical
    property is then split into ``group.NAME;params`` and its value at the
    first ``:`` outside quotes. PHOTO, LOGO, SOUND and KEY are dropped whole;
    every other property counts against the card budget unfolded.

    Only kept properties are retained, so memory stays bounded by the card
    budget plus one property head, however large a skipped value is.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lines: list[str] = []
        self.size = 0  # kept lines plus one line break each
        self.overflowed = False
        self.version21 = False
        self._reset_property()

    def _reset_property(self, *, present: bool = False) -> None:
        self.present = present  # whether a property is open
        self.decided = False  # whether its head has been read
        self.skipped = False
        self.head = ""
        self.quoted = False
        self.encoding: frozenset[str] = frozenset()
        self.base64_open = False
        self.soft_break = False
        self.parts: list[str] = []
        self.property_size = 1  # its line break in the card body

    def start_line(self, text: str) -> None:
        """Add the start of a physical line (or the whole line)."""
        if self.overflowed:
            return
        start = text[:_WHOLE_LINE_START]
        if self.present and text[:1] in (" ", "\t"):
            self.property_size += 1
            self._add(text if self.version21 else text[1:])
        elif self.soft_break and self.encoding & _QUOTED_PRINTABLE_PARAMETERS:
            self._drop_soft_break()
            self.property_size += 1
            self._add(text)
        elif self.base64_open and self.encoding & _BASE64_PARAMETERS and (
            not start.strip() or _BASE64_LINE.fullmatch(start)
        ):
            # A blank line ends a vCard 2.1 BASE64 value.
            self.base64_open = bool(start.strip())
            self.property_size += 1
            self._add(text)
        else:
            self._close_property()
            self._reset_property(present=True)
            self._add(text)
        self.soft_break = False
        self._note_line_end(text)

    def add_piece(self, text: str) -> None:
        """Add a further piece of a physical line a bounded reader cut."""
        if not self.overflowed:
            self._add(text)
            self._note_line_end(text)

    def finish(self) -> str | None:
        """Return the card body, or ``None`` if it exceeded the budget."""
        self._close_property()
        return None if self.overflowed else "\n".join(self.lines)

    def _note_line_end(self, text: str) -> None:
        stripped = text.rstrip()
        if stripped:
            self.soft_break = stripped.endswith("=")

    def _drop_soft_break(self) -> None:
        while self.parts and not self.parts[-1].rstrip():
            self.property_size -= len(self.parts.pop())
        if self.parts:
            last = self.parts[-1].rstrip()
            last = last[:-1] if last.endswith("=") else last
            self.property_size -= len(self.parts[-1]) - len(last)
            self.parts[-1] = last

    def _add(self, text: str) -> None:
        if self.skipped:
            return
        if text:
            self.parts.append(text)
            self.property_size += len(text)
        if not self.decided:
            self._read_head(text)
        if self.decided and not self.skipped:
            self._check_budget()

    def _read_head(self, text: str) -> None:
        room = _MAX_HEAD_CHARS - len(self.head)
        scanned = text[:room]
        for index, char in enumerate(scanned):
            if char == '"':
                self.quoted = not self.quoted
            elif char == ":" and not self.quoted:
                self._decide(self.head + scanned[:index])
                return
        self.head += scanned
        if len(self.head) >= _MAX_HEAD_CHARS or self.property_size > _MAX_HEAD_CHARS:
            self.decided = True  # no property head: kept as text

    def _decide(self, head: str) -> None:
        self.decided = True
        self.head = ""
        name, *parameters = _split_unquoted(head, ";")
        self.encoding = frozenset(part.strip().upper() for part in parameters)
        self.base64_open = bool(self.encoding & _BASE64_PARAMETERS)
        if name.strip().rsplit(".", 1)[-1].upper() in _SKIPPED_PROPERTIES:
            self.skipped = True
            self.parts = []

    def _check_budget(self) -> None:
        if self.size + self.property_size > self.limit:
            self.overflowed = True
            self.lines = []
            self.parts = []

    def _close_property(self) -> None:
        if not self.present or self.overflowed or self.skipped:
            return
        self.decided = True
        self._check_budget()
        if self.overflowed:
            return
        line = "".join(self.parts)
        self.lines.append(line)
        self.size += self.property_size
        name, _, value = line.partition(":")
        if name.strip().upper() == "VERSION":
            self.version21 = value.strip() == "2.1"
        self.parts = []


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
    file opened with universal newlines or ``iter_bounded_lines``. Lines are
    split further at the other ``str.splitlines()`` boundaries (U+2028, form
    feed, and so on), so both forms see the same lines. Iterating a file
    keeps only the current line and card in memory instead of the whole
    phonebook plus its split copy.

    Each body holds the card's unfolded properties, one per line (see
    ``_Card``). PHOTO, LOGO, SOUND, and KEY properties are skipped whole and
    do not count against ``max_card_chars``: nothing here reads them, and a
    large contact picture must not discard the card's name and addresses.
    ``BEGIN:VCARD`` and ``END:VCARD`` always delimit cards, even inside a
    malformed value, so a broken property never reaches another card.
    """
    selected_maximum = max(0, int(maximum))
    selected_card_limit = max(0, int(max_card_chars))
    yielded = 0
    card: _Card | None = None

    for line, continued, complete in _line_pieces(blob):
        if continued:
            if card is not None:
                card.add_piece(line)
            continue
        # A cut piece is never a whole marker line.
        marker = line.strip().casefold() if complete else ""
        if card is not None and line[:1] in (" ", "\t") and card.present:
            marker = ""  # a folded continuation is never a marker
        if marker == "begin:vcard":
            card = _Card(selected_card_limit)
            continue
        if marker == "end:vcard":
            body = card.finish() if card is not None else None
            card = None
            if body is not None:
                yield body
                yielded += 1
                if yielded >= selected_maximum:
                    return
            continue
        if card is not None:
            card.start_line(line)
