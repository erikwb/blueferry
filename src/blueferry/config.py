"""Environment-backed configuration and private runtime paths."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from blueferry.private_files import read_private_text

MAX_CONFIG_FILE_BYTES = 64 * 1024
LOCAL_ENV_KEYS = frozenset({
    "BLUEFERRY_MAC",
    "BLUEFERRY_ADAPTER",
    "BLUEFERRY_ANCS_ENABLED",
    "BLUEFERRY_SHOW_NOTIFICATION_CONTENT",
    "BLUEFERRY_NOTIFICATION_TIMEOUT_MS",
    "BLUEFERRY_HISTORY_RETENTION_DAYS",
    "BLUEFERRY_HISTORY_MAX_EVENTS",
    "BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES",
})
CONFIG_DIR: Path = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "blueferry"
LOCAL_ENV_PATH: Path = CONFIG_DIR / "local.env"
# Preserve which values genuinely came from the process environment before
# ``_load_local_env`` copies file-backed values into ``os.environ``. Runtime
# configuration checks must continue to honor explicit environment overrides.
EXPLICIT_ENV_KEYS = frozenset(key for key in LOCAL_ENV_KEYS if key in os.environ)


def read_local_env(path: Path | None = None) -> dict[str, str]:
    """Read only supported BlueFerry variables from the private config."""
    try:
        lines = read_private_text(
            path or LOCAL_ENV_PATH, maximum_bytes=MAX_CONFIG_FILE_BYTES
        ).splitlines()
    except (OSError, UnicodeError, ValueError):
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in LOCAL_ENV_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _load_local_env() -> None:
    """Load supported local settings before reading module-level values.

    The daemon and CLI parse this file themselves; the systemd unit
    deliberately does not source it into the process environment.

    Anything already in os.environ wins — explicit env > local.env."""
    for key, value in read_local_env().items():
        os.environ.setdefault(key, value)


_load_local_env()


# ---- target device ------------------------------------------------------

_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
_ADAPTER_RE = re.compile(r"^hci[0-9]+$")


def is_valid_mac(value: str) -> bool:
    return bool(_MAC_RE.fullmatch(str(value).strip().upper()))


def is_valid_adapter(value: str) -> bool:
    return bool(_ADAPTER_RE.fullmatch(str(value).strip()))


def _configured_mac() -> str:
    value = os.environ.get("BLUEFERRY_MAC", "").strip().upper()
    return value if is_valid_mac(value) else "AA:BB:CC:DD:EE:FF"


def _configured_adapter() -> str:
    value = os.environ.get("BLUEFERRY_ADAPTER", "hci0").strip()
    return value if is_valid_adapter(value) else "hci0"


def current_target() -> tuple[str, str]:
    """Read the target that a newly started process would use right now."""
    values = read_local_env()
    mac = (
        os.environ.get("BLUEFERRY_MAC", "")
        if "BLUEFERRY_MAC" in EXPLICIT_ENV_KEYS
        else values.get("BLUEFERRY_MAC", "")
    ).strip().upper()
    adapter = (
        os.environ.get("BLUEFERRY_ADAPTER", "hci0")
        if "BLUEFERRY_ADAPTER" in EXPLICIT_ENV_KEYS
        else values.get("BLUEFERRY_ADAPTER", "hci0")
    ).strip()
    return (
        mac if is_valid_mac(mac) else "",
        adapter if is_valid_adapter(adapter) else "hci0",
    )


IPHONE_MAC: str = _configured_mac()
"""BD_ADDR of the paired iPhone. Set BLUEFERRY_MAC env var to your
iPhone's MAC, or put it in ~/.config/blueferry/local.env. The default is a
placeholder — `doctor`
will refuse to pass until you've overridden it."""

ADAPTER: str = _configured_adapter()
"""Local Bluetooth adapter."""

# ---- BlueZ pairing identity (see PROTOCOL.md) ---------------------------

# Class-of-Device: A/V Hands-Free Device. iOS surfaces MAP/PBAP toggles
# only when the adapter presents itself with this CoD class.
COD_MAJOR: int = 4   # Audio/Video
COD_MINOR: int = 8   # = bits 7-2 → 0x02 = Hands-Free Device

ANCS_SOLICIT_UUID: str = "7905F431-B5CE-4E99-A40F-4B1E122D00D0"
"""Apple Notification Center Service UUID. Used in the BLE advert's
SolicitUUIDs field and by the ANCS GATT client."""

BLE_ADVERT_LOCAL_NAME: str = "BlueFerry"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


ANCS_ENABLED: bool = _env_bool("BLUEFERRY_ANCS_ENABLED", True)
"""Whether the daemon should connect and subscribe to ANCS over LE.

The pairing solicitation advertisement is deliberately independent: even
compatibility mode broadcasts it so iOS exposes MAP/PBAP permissions.
"""

SHOW_NOTIFICATION_CONTENT: bool = _env_bool(
    "BLUEFERRY_SHOW_NOTIFICATION_CONTENT", True
)
NOTIFICATION_TIMEOUT_MS: int = _env_int(
    "BLUEFERRY_NOTIFICATION_TIMEOUT_MS", 8_000, 1_000, 60_000
)
HISTORY_RETENTION_DAYS: int = _env_int(
    "BLUEFERRY_HISTORY_RETENTION_DAYS", 30, 1, 3650
)
HISTORY_MAX_EVENTS: int = _env_int(
    "BLUEFERRY_HISTORY_MAX_EVENTS", 10_000, 100, 1_000_000
)
HISTORY_MAX_PAYLOAD_BYTES: int = _env_int(
    "BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES",
    256 * 1024 * 1024,
    16 * 1024 * 1024,
    2 * 1024 * 1024 * 1024,
)

# ---- runtime paths ------------------------------------------------------

_state_home = Path(
    os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
) / "blueferry"

STATE_DIR: Path = _state_home
EVENTS_DB: Path = _state_home / "events.sqlite"
CONTACTS_DB: Path = _state_home / "contacts.sqlite"

SETTINGS_JSON: Path = CONFIG_DIR / "settings.json"

# The state dir holds message bodies and the whole phonebook, so it is
# owner-only. 0700 / 0600 — never let another local account read it.
STATE_DIR_MODE = 0o700
STATE_FILE_MODE = 0o600


def ensure_dirs() -> None:
    """Create the state dir owner-only, and repair the mode if it predates
    this (earlier versions created it 0755, leaking messages + contacts to
    every local account). Also tightens files already sitting in it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
    if STATE_DIR.is_symlink():
        raise PermissionError(f"private state directory is a symlink: {STATE_DIR}")
    state = STATE_DIR.stat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid():
        raise PermissionError(f"private state directory has the wrong owner/type: {STATE_DIR}")
    if state.st_mode & 0o777 != STATE_DIR_MODE:
        STATE_DIR.chmod(STATE_DIR_MODE)
    if STATE_DIR.stat().st_mode & 0o777 != STATE_DIR_MODE:
        raise PermissionError(f"could not secure private state directory: {STATE_DIR}")

    for path in (EVENTS_DB, CONTACTS_DB):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise PermissionError(f"private state file is a symlink: {path}")
        current = path.stat()
        if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid():
            raise PermissionError(f"private state file has the wrong owner/type: {path}")
        if current.st_mode & 0o777 != STATE_FILE_MODE:
            path.chmod(STATE_FILE_MODE)
        if path.stat().st_mode & 0o777 != STATE_FILE_MODE:
            raise PermissionError(f"could not secure private state file: {path}")


def open_state_file(path: Path, mode: str = "a"):
    """Open a file under STATE_DIR, creating it 0600 rather than 0644.

    `Path.open` honours the umask, which on most desktops yields 0644. We
    pass an explicit opener instead so message content is never group- or
    world-readable even on first creation.
    """
    if path.parent == STATE_DIR:
        ensure_dirs()
    else:
        # Explicit alternate paths are used by tests and import/export tools.
        # The file invariant still applies without mutating the configured
        # runtime state directory as a side effect.
        path.parent.mkdir(parents=True, exist_ok=True)

    def _opener(p, flags):
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(p, flags | nofollow, STATE_FILE_MODE)
        try:
            os.fchmod(descriptor, STATE_FILE_MODE)
            current = os.fstat(descriptor)
            if (not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.getuid()
                    or current.st_mode & 0o777 != STATE_FILE_MODE):
                raise PermissionError(f"could not secure private state file: {p}")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    # builtins.open, not Path.open — the latter takes no `opener`.
    return open(path, mode, encoding="utf-8", opener=_opener)

# ---- dbus paths used in the daemon --------------------------------------

BLE_ADVERT_DBUS_PATH: str = "/io/weirdware/BlueFerry/ancs_advert"
