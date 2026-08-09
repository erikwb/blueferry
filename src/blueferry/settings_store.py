"""Small atomic store shared by daemon-owned user preferences."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blueferry import config
from blueferry.private_files import atomic_write_private_text, read_private_text


class SettingsStore:
    """Read and atomically update the owner-only settings document."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.SETTINGS_JSON

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(read_private_text(
                self.path, maximum_bytes=config.MAX_CONFIG_FILE_BYTES
            ))
        except (OSError, UnicodeError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def update(self, **values: Any) -> None:
        payload = self.read()
        payload.update(values)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        atomic_write_private_text(
            self.path,
            encoded,
            maximum_bytes=config.MAX_CONFIG_FILE_BYTES,
        )
