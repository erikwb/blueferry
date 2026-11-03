"""A combo row for the call speaker or microphone (GTK).

Used twice: in Settings > Calls & Audio for every call
(``Calls1.SetCallAudioDevice``) and under the running calls for the current
call only (``SetCallAudioDeviceForCall``). Rows, options and wording come
from :mod:`blueferry.call_audio_view`, which the Qt and terminal clients
share.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gi.repository import Adw, Gtk


class DeviceRow:
    """One ``Adw.ComboRow``; ``on_choice(direction, node_name)`` on user picks."""

    def __init__(self, on_choice: Callable[[str, str], None]) -> None:
        self._on_choice = on_choice
        self._model = Gtk.StringList()
        self.row = Adw.ComboRow(model=self._model, use_markup=False)
        self._direction = ""
        self._values: list[str] = []
        self._applying = False
        self.row.connect("notify::selected", self._selected)

    @property
    def values(self) -> list[str]:
        return list(self._values)

    def show(self, view: Mapping[str, Any], *, subtitle: str = "") -> None:
        """Show a row of ``call_audio_view.settings_view``/``call_view``.

        Only what differs is set, so a status refresh does not disturb an
        open dropdown.
        """
        options = [item for item in view.get("options") or [] if isinstance(item, Mapping)]
        labels = [str(item.get("label") or "") for item in options]
        self._applying = True
        try:
            self._direction = str(view.get("direction") or "")
            title = str(view.get("label") or "")
            if self.row.get_title() != title:
                self.row.set_title(title)
            if (self.row.get_subtitle() or "") != subtitle:
                self.row.set_subtitle(subtitle)
            current = [self._model.get_string(i) for i in range(self._model.get_n_items())]
            if labels != current:
                self._model.splice(0, self._model.get_n_items(), labels)
            self._values = [str(item.get("value") or "") for item in options]
            index = view.get("index")
            selected = index if isinstance(index, int) and 0 <= index < len(labels) else 0
            if labels and self.row.get_selected() != selected:
                self.row.set_selected(selected)
        finally:
            self._applying = False

    def _selected(self, _row, _property) -> None:
        if self._applying:
            return
        index = self.row.get_selected()
        if 0 <= index < len(self._values) and self._direction:
            self._on_choice(self._direction, self._values[index])
