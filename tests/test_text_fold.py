"""Search folding: case, accents and Unicode spelling variants."""
from __future__ import annotations

import unicodedata

import pytest

from blueferry.text_fold import fold, folded_with_offsets, original_span

NFC = unicodedata.normalize("NFC", "Müller")
NFD = unicodedata.normalize("NFD", "Müller")


@pytest.mark.parametrize("text", [NFC, NFD, "Muller", "MÜLLER", "\uff2düller"])
def test_spellings_of_one_name_fold_alike(text) -> None:
    assert fold(text) == "muller"


def test_sharp_s_and_ligatures_fold_to_plain_letters() -> None:
    assert fold("Straße") == "strasse"
    assert fold("ﬁne") == "fine"


@pytest.mark.parametrize("text", [NFC, NFD, "Muller"])
def test_spans_map_back_to_the_original(text) -> None:
    folded, origin = folded_with_offsets(text)
    position = folded.find("mul")
    start, end = original_span(text, origin, position, 3)
    assert fold(text[start:end]) == "mul"
    # The combining mark of a decomposed ü belongs to the match.
    assert text[start:end] in {"Mül", "Mul", unicodedata.normalize("NFD", "Mül")}


def test_span_after_a_sharp_s_is_not_shifted() -> None:
    text = "Straße Bern lunch"
    folded, origin = folded_with_offsets(text)
    position = folded.find("lunch")
    start, end = original_span(text, origin, position, len("lunch"))
    assert text[start:end] == "lunch"
    position = folded.find("s")
    assert original_span(text, origin, folded.find("ss"), 2) == (4, 5)
    assert text[slice(*original_span(text, origin, position, 1))] == "S"
