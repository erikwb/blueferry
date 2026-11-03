"""The saved phone-calls opt-in (``settings.json``, seeded by local.env)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from blueferry import config
from blueferry.calls.audio_devices import valid_node_name
from blueferry.errors import InvalidArgumentsError
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)

ENABLED_KEY = "calls_enabled"
ENV_NAME = "BLUEFERRY_CALLS_ENABLED"


class CallsSettings:
    """Persist whether the optional phone-call integration is switched on.

    ``BLUEFERRY_CALLS_ENABLED`` in local.env provides the initial value, so
    existing setups keep working. A value saved through the D-Bus API (the Qt
    settings checkbox, ``blueferry calls enable``) takes precedence.
    """

    def __init__(self, path: Path | None = None, *, default_enabled: bool | None = None) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read().get(ENABLED_KEY)  # never raises
        seeded = config.CALLS_ENABLED if default_enabled is None else default_enabled
        self._enabled = stored if isinstance(stored, bool) else bool(seeded)
        if (
            default_enabled is None and isinstance(stored, bool)
            and ENV_NAME in os.environ and bool(seeded) != stored
        ):
            log.info(
                "%s is ignored because a phone-calls preference was saved in "
                "settings.json (using %s)", ENV_NAME, stored,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("calls enabled must be a boolean")
        self._settings.update(**{ENABLED_KEY: enabled})
        self._enabled = enabled
        return enabled


def calls_enabled() -> bool:
    """The effective opt-in, for code outside the daemon (pairing)."""
    return CallsSettings().enabled


AUDIO_KEYS = {"output": "calls_audio_output", "input": "calls_audio_input"}


class CallAudioSettings:
    """The saved speaker and microphone for calls (``settings.json``).

    Each is a PipeWire ``node.name``, which stays the same across reboots
    and replugging; "" means "same as the system". Only read while calls
    are on.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        stored = self._settings.read()  # never raises
        self._values = {
            direction: _saved_name(stored.get(key)) for direction, key in AUDIO_KEYS.items()
        }

    def get(self, direction: str) -> str:
        return self._values[direction]

    def set(self, direction: str, name: str) -> str:
        if direction not in AUDIO_KEYS or not isinstance(name, str):
            raise ValueError("unknown audio direction or device")
        self._settings.update(**{AUDIO_KEYS[direction]: name})
        self._values[direction] = name
        return name


def _saved_name(value: object) -> str:
    """A hand-edited or corrupt value reads as "same as the system"."""
    try:
        return valid_node_name(value)
    except InvalidArgumentsError:
        return ""
