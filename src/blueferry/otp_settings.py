"""The saved one-time code auto-copy opt-in (``settings.json``).

``BLUEFERRY_OTP_AUTOCOPY`` in local.env only provides the initial value, so
setups that opted in through local.env keep working. A choice saved through
the D-Bus API (the clients' "Copy one-time codes" checkbox, ``blueferry otp
enable``) takes precedence, like the phone-calls and away-lock preferences.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from blueferry import config
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

ENABLED_KEY = "otp_autocopy"
ENV_NAME = "BLUEFERRY_OTP_AUTOCOPY"


class OtpSettings:
    """Read and persist whether one-time codes are copied to the clipboard."""

    def __init__(self, path: Path | None = None, *, log_overrides: bool = False) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read().get(ENABLED_KEY)  # never raises
        seeded = bool(config.OTP_AUTOCOPY)
        self._enabled = stored if isinstance(stored, bool) else seeded
        overridden = isinstance(stored, bool) and seeded != stored
        if log_overrides and overridden and ENV_NAME in os.environ:
            log.info(
                "%s is ignored because a one-time code preference was saved in "
                "settings.json (using %s)", ENV_NAME, stored,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("one-time code auto-copy must be a boolean")
        self._settings.update(**{ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled
