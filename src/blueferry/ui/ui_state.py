"""The GTK window's own small preferences, e.g. whether **Plugins & Tools**
is unfolded.

They live in ``gtk-ui.json`` next to ``settings.json`` (the same private,
atomically written :class:`blueferry.settings_store.SettingsStore` format),
not in the daemon's ``settings.json``: the daemon writes that file from
another process. Reads and writes run on a worker, so the GTK thread never
touches the disk; one worker keeps the writes in order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from blueferry import config
from blueferry.client_tasks import TaskRunner
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)
EXTRAS_EXPANDED = "plugins_tools_expanded"


def default_path() -> Path:
    return config.CONFIG_DIR / "gtk-ui.json"


class UiState:
    def __init__(self, runner: TaskRunner, path: Path | None = None) -> None:
        self._runner = runner
        self._store = SettingsStore(path or default_path())

    def load(self, done: Callable[[dict[str, Any]], None]) -> None:
        """``done(values)`` on the UI thread; ``{}`` when unreadable."""
        self._runner.submit(
            self._store.read,
            lambda values: done(values if isinstance(values, dict) else {}),
            lambda _message: done({}),
        )

    def save(self, **values: Any) -> None:
        self._runner.submit(
            lambda: self._store.update(**values), None,
            lambda message: log.warning("could not save the window state: %s", message),
        )
