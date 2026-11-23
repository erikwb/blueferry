"""Messages for declining a ringing call with a text ("quick replies").

``Calls1.DeclineWithMessage`` declines the call over HFP and then sends a
text to the caller's number over MAP, the same way as ``Send``. The saved
replies live in ``settings.json`` (``calls_quick_replies``); clients offer
them next to their own free text. Without a saved list the defaults below
apply, translated with the daemon's locale; an empty list means "own text
only". The whole feature exists only while calls are on.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from blueferry import config
from blueferry.errors import InvalidArgumentsError
from blueferry.i18n import _
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

SETTINGS_KEY = "calls_quick_replies"
MAX_REPLIES = 5
# One SMS worth of characters keeps a reply short enough for a popup button.
MAX_REPLY_CHARS = 160


def default_replies() -> list[str]:
    """The built-in replies, translated now (English without a catalog)."""
    return [
        _("Can't talk now. I'll call you back."),
        _("I'm in a meeting. I'll call you back later."),
        _("Can't talk now. Please send me a message."),
    ]


def valid_reply(value: object) -> str:
    """One reply: plain one-line text of 1 to ``MAX_REPLY_CHARS`` characters."""
    if not isinstance(value, str):
        raise InvalidArgumentsError("a quick reply must be a string")
    text = value.strip()
    if not text:
        raise InvalidArgumentsError("a quick reply must not be empty")
    if len(text) > MAX_REPLY_CHARS:
        raise InvalidArgumentsError(f"a quick reply is longer than {MAX_REPLY_CHARS} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise InvalidArgumentsError("a quick reply must be a single line of text")
    return text


def valid_replies(values: object) -> list[str]:
    """A whole list: at most ``MAX_REPLIES``, each valid, no duplicates."""
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise InvalidArgumentsError("quick replies must be a list of strings")
    if len(values) > MAX_REPLIES:
        raise InvalidArgumentsError(f"at most {MAX_REPLIES} quick replies can be saved")
    replies = [valid_reply(value) for value in values]
    if len(set(replies)) != len(replies):
        raise InvalidArgumentsError("quick replies must differ from each other")
    return replies


class QuickReplySettings:
    """The saved replies (``settings.json``); defaults while none are saved."""

    def __init__(self, path: Path | None = None) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read()  # never raises
        self._saved: list[str] | None = None
        if SETTINGS_KEY in stored:
            try:
                self._saved = valid_replies(stored[SETTINGS_KEY])
            except InvalidArgumentsError:
                # A hand-edited or corrupt list reads as the defaults.
                log.warning("ignoring invalid saved quick replies")

    def get(self) -> list[str]:
        return list(self._saved) if self._saved is not None else default_replies()

    def set(self, values: object) -> list[str]:
        replies = valid_replies(values)
        self._settings.update(**{SETTINGS_KEY: replies})
        self._saved = replies
        return list(replies)
