"""The phone card's quick switches, also used on the Calls tab.

"Sound on this computer" moves the iPhone's sound (``SetPhoneAudioRoute``)
and "Lock when I walk away" switches the away lock, like the switches on
the Qt phone card (``PhoneCard.qml``). Each controller binds one
``Adw.SwitchRow`` to the daemon status: the row follows the reported state,
not the click, and is insensitive while a change is saved.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw

from blueferry import phone_link_view
from blueferry.i18n import _
from blueferry.phone_audio_view import phone_audio
from blueferry.proximity_lock import clamp_grace

Toast = Callable[[str], None]


class _StatusSwitch:
    def __init__(self, row: Adw.SwitchRow, toast: Toast) -> None:
        self.row = row
        self._toast = toast
        self._status: dict = {}
        self._applying = False
        self._saving = False
        row.connect("notify::active", self._toggled)

    def apply(self, status: dict) -> None:
        """Show a status read (``BackendStatus.to_dict()``)."""
        self._status = status
        active, subtitle, sensitive = self._view(status)
        self._applying = True
        try:
            if not self._saving:
                self.row.set_active(active)
            self.row.set_subtitle(subtitle)
            self.row.set_sensitive(sensitive and not self._saving)
        finally:
            self._applying = False

    def _toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._applying:
            return
        self._saving = True
        row.set_sensitive(False)
        self._save(row.get_active(), self._saved, self._failed)

    def _saved(self, _value) -> None:
        self._saving = False
        # The next StatusChanged shows the result; until then keep the click.
        self.row.set_sensitive(self._view(self._status)[2])

    def _failed(self, message: str) -> None:
        self._saving = False
        self._toast(message)
        self.apply(self._status)

    def _view(self, status: dict) -> tuple[bool, str, bool]:
        raise NotImplementedError

    def _save(self, active: bool, done, failed) -> None:
        raise NotImplementedError


class PhoneAudioSwitch(_StatusSwitch):
    """Sound on this computer: the iPhone's audio route."""

    def __init__(self, row: Adw.SwitchRow, client, toast: Toast) -> None:
        super().__init__(row, toast)
        self._client = client

    def _view(self, status: dict) -> tuple[bool, str, bool]:
        audio = phone_audio(status)
        sensitive = (
            status.get("daemon") is True and audio["available"] and not audio["pending"]
        )
        return bool(audio["on_pc"]), str(audio["hint"]), bool(sensitive)

    def _save(self, active: bool, done, failed) -> None:
        self._client.set_phone_audio_route_async("pc" if active else "phone", done, failed)


class ProximitySwitch(_StatusSwitch):
    """Lock when I walk away: the away lock with its saved grace period."""

    def __init__(self, row: Adw.SwitchRow, client, toast: Toast) -> None:
        super().__init__(row, toast)
        self._client = client

    def _view(self, status: dict) -> tuple[bool, str, bool]:
        # Like the Qt card: the subtitle only says why the switch is unavailable.
        hint = phone_link_view.feature_hints(status).get("proximity", "")
        sensitive = status.get("daemon") is True and "proximity_lock" in status
        return status.get("proximity_lock_enabled") is True, hint, sensitive

    def _save(self, active: bool, done, failed) -> None:
        grace = clamp_grace(self._status.get("proximity_lock_grace_sec"))
        self._client.set_proximity_lock_async(active, grace, done, failed)


def sound_row() -> Adw.SwitchRow:
    return Adw.SwitchRow(title=_("Sound on This Computer"), use_markup=False)


def away_lock_row() -> Adw.SwitchRow:
    return Adw.SwitchRow(title=_("Lock When I Walk Away"), use_markup=False)
