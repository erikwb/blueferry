"""Linear, resource-bounded extraction of vCard blocks."""
from __future__ import annotations

from collections.abc import Iterator

from blueferry.limits import MAX_VCARD_CHARS


def iter_vcard_bodies(
    blob: str,
    *,
    maximum: int,
    max_card_chars: int = MAX_VCARD_CHARS,
) -> Iterator[str]:
    """Yield complete vCard bodies without rescanning malformed prefixes.

    A nested ``BEGIN:VCARD`` restarts the pending card. This both recovers from
    malformed input and ensures a run of unterminated begin markers stays
    linear rather than making a regex retry the remainder for every marker.
    Oversized cards are discarded through their matching terminator.
    """
    selected_maximum = max(0, int(maximum))
    selected_card_limit = max(0, int(max_card_chars))
    yielded = 0
    active = False
    overflowed = False
    size = 0
    lines: list[str] = []

    for line in blob.splitlines():
        marker = line.strip().casefold()
        if marker == "begin:vcard":
            active = True
            overflowed = False
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
            size = 0
            lines = []
            continue
        if not active or overflowed:
            continue
        size += len(line) + 1
        if size > selected_card_limit:
            overflowed = True
            lines = []
            continue
        lines.append(line)
