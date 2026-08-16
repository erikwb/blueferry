"""Presentation-boundary helpers for text received from remote devices."""
from __future__ import annotations

import unicodedata

_BIDI_CONTROL_CODEPOINTS = frozenset({
    0x061C,  # Arabic Letter Mark
    0x200E, 0x200F,  # left/right-to-left marks
    *range(0x202A, 0x202F),  # embedding/override and pop formatting
    *range(0x2066, 0x206A),  # directional isolates and pop isolate
})


def terminal_text(value: object) -> str:
    """Neutralize terminal controls and invisible bidi overrides.

    Newlines are retained for the caller to format. Tabs and all other C0/C1
    controls become a visible replacement character instead of being emitted.
    Unicode line and paragraph separators are also replaced so they cannot
    create extra terminal rows while bypassing newline handling.
    """
    output = []
    for character in str(value or ""):
        codepoint = ord(character)
        if character == "\n":
            output.append(character)
        elif (
            unicodedata.category(character) == "Cc"
            or unicodedata.category(character) in {"Zl", "Zp"}
            or 0x7F <= codepoint <= 0x9F
            or codepoint in _BIDI_CONTROL_CODEPOINTS
        ):
            output.append("�")
        else:
            output.append(character)
    return "".join(output)
