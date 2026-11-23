"""Speaker/microphone selects for phone calls in the Textual client.

Used in Options > Calls & Audio for every call (``SetCallAudioDevice``) and
in the calls panel for the running call only (``SetCallAudioDeviceForCall``).
Rows and wording come from :mod:`blueferry.call_audio_view`, shared with the
Qt and GTK clients. Device names come from PipeWire and are shown through
``terminal_text``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from textual.widgets import Select
from textual.widgets.select import NoSelection

from blueferry.text_safety import terminal_text


class DeviceSelect(Select[str]):
    """One direction's devices. The Changed that Select sends from its own
    Mount handler is not a user choice and is held back; the screens render
    the daemon's value inside ``prevent(Select.Changed)``, so every later
    Changed comes from the user."""

    def __init__(self, *, id: str, classes: str = "") -> None:
        super().__init__([("", "")], allow_blank=False, value="", id=id, classes=classes)
        self.direction = ""
        self.choices: list[tuple[str, str]] = [("", "")]

    # Overrides Select's private watcher, exactly like tui_options.PolicySelect
    # (checked against Textual 8.2); revisit both when Textual changes Select.
    def _watch_value(self, value: str | NoSelection) -> None:
        if self.is_mounted:
            super()._watch_value(value)
            return
        with self.prevent(Select.Changed):
            super()._watch_value(value)

    def show(self, row: Mapping[str, Any], selected: str | None = None) -> None:
        """Show a row of ``call_audio_view`` with ``selected`` (else its own)."""
        self.direction = str(row.get("direction") or "")
        choices = [
            (terminal_text(item.get("label")).replace("\n", " "), str(item.get("value") or ""))
            for item in row.get("options") or [] if isinstance(item, Mapping)
        ] or [("", "")]
        values = {value for _label, value in choices}
        wanted = str(row.get("selected") or "") if selected is None else selected
        with self.prevent(Select.Changed):
            if choices != self.choices:
                self.set_options(choices)
                self.choices = choices
            self.value = wanted if wanted in values else choices[0][1]
