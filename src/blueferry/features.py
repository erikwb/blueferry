"""Switches the settings UIs may change: Messages1.GetFeatures/SetFeature.

Each feature is one boolean ``local.env`` variable from a fixed allowlist.
SetFeature stores the choice in settings.json (``"features"``), which
:mod:`blueferry.config` applies before local.env the next time the daemon
starts; an explicit process environment still wins. The daemon reads these
values once at start-up, so a change reports ``restart-required`` until the
service restarts.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blueferry import config
from blueferry.settings_store import SettingsStore


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    variable: str
    default: bool


# Only switches without a runtime setter of their own: calls, call history,
# media, the away lock, the battery warning and the ANCS action buttons
# already have one (settings.json via their own D-Bus methods). The list
# itself lives in config.FEATURE_SWITCHES, which config needs at import time.
FEATURES: tuple[Feature, ...] = tuple(
    Feature(name, variable, default) for name, variable, default in config.FEATURE_SWITCHES
)
BY_NAME: Mapping[str, Feature] = {feature.name: feature for feature in FEATURES}


def running_values() -> dict[str, bool]:
    """What this daemon process uses, as computed by config at start-up."""
    return {
        "show_notification_content": config.SHOW_NOTIFICATION_CONTENT,
        "mark_read_on_dismiss": config.MARK_READ_ON_DISMISS,
    }


class FeatureSettings:
    """Read and store the switches; the daemon's view of them."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        running: Callable[[], Mapping[str, bool]] = running_values,
        explicit: frozenset[str] | None = None,
        local_env: Callable[[], Mapping[str, str]] = config.read_local_env,
    ) -> None:
        self._path = path or config.SETTINGS_JSON
        self._store = SettingsStore(self._path)
        self._running = running
        self._explicit = config.EXPLICIT_ENV_KEYS if explicit is None else explicit
        self._local_env = local_env

    def _stored(self) -> dict[str, bool]:
        return config.read_feature_settings(self._path)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per feature: value (after restart), running, source, restart_required."""
        stored, running, local = self._stored(), self._running(), self._local_env()
        result: dict[str, dict[str, Any]] = {}
        for feature in FEATURES:
            now = bool(running.get(feature.name, feature.default))
            if feature.variable in self._explicit:
                source, value = "environment", now
            elif feature.variable in stored:
                source, value = "settings", stored[feature.variable]
            elif feature.variable in local:
                source, value = "local.env", now
            else:
                source, value = "default", feature.default
            result[feature.name] = {
                "value": value,
                "running": now,
                "source": source,
                "variable": feature.variable,
                "restart_required": value != now,
            }
        return result

    def set(self, name: str, enabled: bool) -> str:
        """Store one switch; ``restart-required``, ``active`` or ``environment``."""
        feature = BY_NAME.get(name)
        if feature is None:
            raise KeyError(name)
        stored = self._stored()
        stored[feature.variable] = bool(enabled)
        self._store.update(**{config.FEATURES_SETTINGS_KEY: dict(sorted(stored.items()))})
        if feature.variable in self._explicit:
            return "environment"
        running = bool(self._running().get(name, feature.default))
        return "active" if running == bool(enabled) else "restart-required"
