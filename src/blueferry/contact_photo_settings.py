"""The saved contact-photo opt-in (``settings.json``).

``BLUEFERRY_CONTACT_PHOTOS`` in local.env only provides the initial value,
so setups that opted in through local.env keep working. A choice saved
through the D-Bus API (the clients' "Show contact photos" checkbox,
``blueferry contact-photos enable``) takes precedence, like the phone-calls
and away-lock preferences.

The daemon publishes the effective value with :func:`set_runtime`, so the
PBAP parser and the contact cache, which run in the daemon process, follow
a change without a restart. Elsewhere (and before the daemon decided) the
local.env seed applies.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

ENABLED_KEY = "contact_photos"
ENV_NAME = "BLUEFERRY_CONTACT_PHOTOS"

_runtime: bool | None = None


def photos_enabled() -> bool:
    """Whether photos are parsed, kept and served right now."""
    return bool(config.CONTACT_PHOTOS) if _runtime is None else _runtime


def set_runtime(enabled: bool | None) -> None:
    """Publish the daemon's effective setting (``None`` resets to the seed)."""
    global _runtime
    _runtime = None if enabled is None else bool(enabled)


class ContactPhotoSettings:
    """Read and persist whether contact photos are kept and shown."""

    def __init__(self, path: Path | None = None, *, log_overrides: bool = False) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read().get(ENABLED_KEY)  # never raises
        seeded = bool(config.CONTACT_PHOTOS)
        self._enabled = stored if isinstance(stored, bool) else seeded
        overridden = isinstance(stored, bool) and seeded != stored
        if log_overrides and overridden and ENV_NAME in os.environ:
            log.info(
                "%s is ignored because a contact-photo preference was saved in "
                "settings.json (using %s)", ENV_NAME, stored,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("contact photos must be a boolean")
        self._settings.update(**{ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled
