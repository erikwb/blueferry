"""Search folding: one spelling for text that a person would call the same.

A query and the text it is matched against are both folded with
:func:`fold`: compatibility forms are unified (NFKD, so full-width letters
and ligatures match their plain forms), accents are dropped (``Muller``
finds ``Müller``, whether the iPhone stored it precomposed or decomposed)
and case is folded (``STRASSE`` finds ``Straße``).

Folding works character by character, so :func:`folded_with_offsets` can
report where each folded character came from and a match in the folded
text maps back to the exact span of the original.
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def _fold_character(character: str) -> str:
    decomposed = unicodedata.normalize("NFKD", character.casefold())
    return "".join(
        part for part in decomposed if unicodedata.category(part) != "Mn"
    )


def fold(text: str) -> str:
    """Case-, accent- and compatibility-folded form of ``text`` for matching."""
    if text.isascii():
        return text.lower()
    return "".join(_fold_character(character) for character in text)


def folded_with_offsets(text: str) -> tuple[str, list[int] | None]:
    """``(fold(text), origin)``: ``origin[i]`` is the index in ``text`` of folded char ``i``.

    ``origin`` is ``None`` when the folding is the identity mapping (ASCII
    text), which saves building it for the common case.
    """
    if text.isascii():
        return text.lower(), None
    parts: list[str] = []
    origin: list[int] = []
    for index, character in enumerate(text):
        folded = _fold_character(character)
        parts.append(folded)
        origin.extend([index] * len(folded))
    return "".join(parts), origin


def original_span(
    text: str, origin: list[int] | None, start: int, length: int,
) -> tuple[int, int]:
    """The ``(start, end)`` in ``text`` of a ``length``-char match at folded ``start``.

    The span covers every original character that contributed to the match,
    including combining marks that follow its last character and fold away.
    """
    if origin is None:
        return start, start + length
    if length <= 0 or start >= len(origin):
        position = origin[start] if start < len(origin) else len(text)
        return position, position
    begin = origin[start]
    end = origin[start + length - 1] + 1
    while end < len(text) and not _fold_character(text[end]):
        end += 1
    return begin, end
