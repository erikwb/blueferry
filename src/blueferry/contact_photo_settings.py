"""The saved contact-photo opt-in (``settings.json``).

``BLUEFERRY_CONTACT_PHOTOS`` in local.env only provides the initial value,
so setups that opted in through local.env keep working. A choice saved
through the D-Bus API (the clients' "Show contact photos" checkbox,
``blueferry contact-photos enable``) takes precedence, like the phone-calls
and away-lock preferences.

The daemon hands ``enabled`` to the contact cache, the contact sync and
storage preparation as a callable, so they follow a change without a
restart.

While settings.json exists but can't be read, the choice is unknown: the
daemon neither erases, fetches nor shows photos until it reads again.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore, UnreadableSettingsError

log = logging.getLogger(__name__)

ENABLED_KEY = "contact_photos"
ENV_NAME = "BLUEFERRY_CONTACT_PHOTOS"


class ContactPhotoSettings:
    """Read and persist whether contact photos are kept and shown."""

    def __init__(self, path: Path | None = None, *, log_overrides: bool = False) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        self._log_overrides = log_overrides
        self._enabled: bool | None = None
        if not self.reload():
            log.warning(
                "the contact-photo preference is unknown until %s can be read",
                self._settings.path.name,
            )

    def reload(self) -> bool:
        """Read the saved choice again; return whether it is known."""
        try:
            stored = self._settings.load().get(ENABLED_KEY)
        except UnreadableSettingsError:
            return False
        seeded = bool(config.CONTACT_PHOTOS)
        self._enabled = stored if isinstance(stored, bool) else seeded
        overridden = isinstance(stored, bool) and seeded != stored
        if self._log_overrides and overridden and ENV_NAME in os.environ:
            log.info(
                "%s is ignored because a contact-photo preference was saved in "
                "settings.json (using %s)", ENV_NAME, stored,
            )
        self._log_overrides = False
        return True

    @property
    def known(self) -> bool:
        return self._enabled is not None

    @property
    def enabled(self) -> bool:
        """On only when the saved choice is known to be on."""
        return self._enabled is True

    @property
    def saved(self) -> bool | None:
        """The choice, or ``None`` while settings.json can't be read."""
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("contact photos must be a boolean")
        self._settings.update(**{ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled
