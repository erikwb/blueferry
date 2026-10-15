"""The per-user autostart entry for ``blueferry-tray`` (opt-in).

Packages install no system-wide autostart entry. The tray menu's "Start at
login" switch, or ``blueferry-tray --autostart on|off``, writes or removes
``$XDG_CONFIG_HOME/autostart/io.weirdware.BlueFerry.Tray.desktop`` as the
XDG Autostart specification describes. Desktop settings that edit the same
file (Plasma's Autostart page, GNOME Tweaks) stay in agreement.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

TRAY_DESKTOP_ID = "io.weirdware.BlueFerry.Tray"
ENTRY_NAME = f"{TRAY_DESKTOP_ID}.desktop"
# Generous for a desktop entry; a larger file is not ours to interpret.
MAX_ENTRY_BYTES = 64 * 1024
ENTRY_MODE = 0o644

# Identical to data/io.weirdware.BlueFerry.Tray.desktop (a test checks it),
# so the autostart copy launches exactly what the application menu does.
ENTRY_TEXT = """\
[Desktop Entry]
Type=Application
Name=BlueFerry Tray
GenericName=iPhone Status
Comment=iPhone status and unread messages in the system tray
Exec=blueferry-tray
Icon=io.weirdware.BlueFerry
Terminal=false
Categories=Network;Qt;KDE;
Keywords=iPhone;Bluetooth;Tray;Messages;
X-KDE-autostart-phase=2
X-GNOME-Autostart-enabled=true
"""


def entry_path(environment: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    config = environment.get("XDG_CONFIG_HOME") or ""
    # The specification ignores a relative XDG_CONFIG_HOME.
    base = Path(config) if os.path.isabs(config) else Path.home() / ".config"
    return base / "autostart" / ENTRY_NAME


def _entry_disables(text: str) -> bool:
    """``Hidden=true`` or a GNOME switch set to false turn an entry off."""
    in_entry = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "Hidden" and value.lower() == "true":
            return True
        if key == "X-GNOME-Autostart-enabled" and value.lower() == "false":
            return True
    return False


def is_enabled(path: Path | None = None) -> bool:
    path = entry_path() if path is None else path
    try:
        if path.stat().st_size > MAX_ENTRY_BYTES:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return not _entry_disables(text)


def set_enabled(enabled: bool, path: Path | None = None) -> None:
    """Write or remove the entry; raises OSError when that fails."""
    path = entry_path() if path is None else path
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, ENTRY_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(ENTRY_TEXT)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
