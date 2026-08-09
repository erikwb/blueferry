"""Translation helpers shared by the Python presentation layers."""
from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "blueferry"
LOCALE_DIR = Path(os.environ.get("BLUEFERRY_LOCALE_DIR", "/usr/share/locale"))

_translation = gettext.translation(
    DOMAIN,
    localedir=LOCALE_DIR,
    fallback=True,
)
_ = _translation.gettext


def ngettext(singular: str, plural: str, count: int) -> str:
    return _translation.ngettext(singular, plural, count)
