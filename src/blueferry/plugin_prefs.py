"""Which plugins the user disabled (``$XDG_CONFIG_HOME/blueferry/plugins.json``).

A tiny reader shared by the plugin manager, the clients and the daemon's
popup bridge; it runs nothing and imports nothing that blocks.
"""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

MAX_SETTINGS_BYTES = 1024 * 1024


def config_path() -> Path:
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(home) / "blueferry" / "plugins.json"


def read_settings(path: Path | None = None) -> dict:
    return read_json(path or config_path())


def read_json(path: Path) -> dict:
    # O_NONBLOCK keeps a FIFO from blocking the open; the fstat below rejects
    # it, and the flag has no effect on reads from a regular file. Symlinks
    # are still followed.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            value = json.loads(stream.read(MAX_SETTINGS_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def disabled_from(settings: Mapping[str, object]) -> frozenset[str]:
    value = settings.get("disabled")
    return frozenset(str(item) for item in value) if isinstance(value, list) else frozenset()


def disabled_plugins(path: Path | None = None) -> frozenset[str]:
    return disabled_from(read_settings(path))
