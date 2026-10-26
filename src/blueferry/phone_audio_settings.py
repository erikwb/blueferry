"""A remembered media route for the selected phone, inactive by default."""
from __future__ import annotations

from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

KEY = "phone_audio_preference"
CHOICES = frozenset({"auto", "pc", "phone"})


class PhoneAudioSettings:
    def __init__(self, mac: str, path: Path | None = None) -> None:
        self.mac = mac.strip().upper()
        self._store = SettingsStore(path)
        saved = self._store.read().get(KEY)
        self.choice = "auto"
        if isinstance(saved, dict) and saved.get("device") == self.mac:
            value = saved.get("route")
            if isinstance(value, str) and value in CHOICES:
                self.choice = value

    def set(self, choice: str) -> None:
        if choice not in CHOICES:
            raise ValueError("audio preference must be 'pc', 'phone' or 'auto'")
        if not config.is_valid_mac(self.mac):
            raise ValueError("select an iPhone before saving an audio preference")
        self._store.update(**{KEY: {"device": self.mac, "route": choice}})
        self.choice = choice
