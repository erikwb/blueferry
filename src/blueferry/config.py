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
    "BLUEFERRY_ANCS_APP_ALLOWLIST",
    "BLUEFERRY_ANCS_APP_BLOCKLIST",
    "BLUEFERRY_ANCS_ACTIONS",
    "BLUEFERRY_ANCS_ACTION_TIMEOUT_MS",
    "BLUEFERRY_SHOW_NOTIFICATION_CONTENT",
    "BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE",
    "BLUEFERRY_CALLS_ENABLED",
    "BLUEFERRY_PHONE_BATTERY_NOTIFY",
    "BLUEFERRY_PHONE_BATTERY_LOW_PERCENT",
    "BLUEFERRY_NOTIFICATION_TIMEOUT_MS",
    "BLUEFERRY_MARK_READ_ON_DISMISS",
    "BLUEFERRY_OTP_AUTOCOPY",
    "BLUEFERRY_OTP_CLEAR_SECONDS",
    "BLUEFERRY_HISTORY_RETENTION_DAYS",
    "BLUEFERRY_HISTORY_MAX_EVENTS",
    "BLUEFERRY_HISTORY_MAX_PAYLOAD_BYTES",
    "BLUEFERRY_PROXIMITY_LOCK",
    "BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC",
    "BLUEFERRY_CONTACT_PHOTOS",
    "BLUEFERRY_CALL_HISTORY_ENABLED",
    "BLUEFERRY_CALL_HISTORY_INTERVAL_SEC",
    "BLUEFERRY_MISSED_CALL_NOTIFICATIONS",
    "BLUEFERRY_MEDIA_CONTROL_ENABLED",
    "BLUEFERRY_MEDIA_MPRIS_ENABLED",
    "BLUEFERRY_TETHER_AUTOCONNECT",
    "BLUEFERRY_TETHER_BACKEND",
    "BLUEFERRY_TETHER_ENABLED",
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


def _env_opt_in(name: str) -> bool:
    """Parse a default-off flag; only an explicit affirmative enables it."""
    value = os.environ.get(name)
    return value is not None and value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


_ANCS_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _env_ancs_app_ids(name: str) -> frozenset[str] | None:
    """Parse one optional comma-separated set of exact ANCS bundle IDs."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return frozenset(
        app_id
        for value in raw.split(",")
        if (app_id := value.strip()) and _ANCS_APP_ID_RE.fullmatch(app_id)
    )


ANCS_ENABLED: bool = _env_bool("BLUEFERRY_ANCS_ENABLED", True)
"""Whether the daemon should connect and subscribe to ANCS over LE.

The pairing solicitation advertisement is deliberately independent: even
compatibility mode broadcasts it so iOS exposes MAP/PBAP permissions.
"""

ANCS_APP_ALLOWLIST: frozenset[str] | None = _env_ancs_app_ids(
    "BLUEFERRY_ANCS_APP_ALLOWLIST"
)
"""Exact bundle IDs allowed to create non-Messages ANCS popups.

``None`` means no allowlist was configured. An explicitly empty value allows
no non-Messages apps. Apple Messages bypasses this popup filter because its
ANCS metadata is required for group-message correlation.
"""

ANCS_APP_BLOCKLIST: frozenset[str] = (
    _env_ancs_app_ids("BLUEFERRY_ANCS_APP_BLOCKLIST") or frozenset()
)
"""Exact bundle IDs denied after the allowlist; block rules take precedence."""


ANCS_ACTIONS: bool = _env_bool("BLUEFERRY_ANCS_ACTIONS", False)
"""Initial value for offering iPhone notification actions as buttons.

A choice saved from a client (settings.json) wins over this value.

Off by default because it changes the desktop notification UI and lets a
click act on the phone. It applies only to non-Messages popups shown by the
"All iPhone Notifications" policy; nothing is invoked without a click.
Labels are chosen by the sending app and can contain content, so actions stay
off while BLUEFERRY_SHOW_NOTIFICATION_CONTENT is false.
"""


def include_ancs_app(app_id: str) -> bool:
    """Return whether one validated non-Messages app passes local rules."""
    selected = str(app_id).strip()
    if not _ANCS_APP_ID_RE.fullmatch(selected):
        return False
    if selected in ANCS_APP_BLOCKLIST:
        return False
    return ANCS_APP_ALLOWLIST is None or selected in ANCS_APP_ALLOWLIST

MEDIA_CONTROL_ENABLED: bool = _env_bool("BLUEFERRY_MEDIA_CONTROL_ENABLED", False)
"""Opt in to iPhone now-playing and media commands over Apple Media Service.

Off by default: AMS subscriptions add LE traffic on the bond that carries
ANCS, and this is outside BlueFerry's messaging core. Requires the full
(ANCS/LE) delivery mode; compatibility mode never connects LE.
"""

MEDIA_MPRIS_ENABLED: bool = _env_bool("BLUEFERRY_MEDIA_MPRIS_ENABLED", False)
"""Initial value for also publishing the iPhone as an MPRIS2 player.

Effective only while media control is on. MPRIS metadata (title, artist,
album) is by design readable by every application in the login session, like
any desktop music player. It is a separate opt-in so enabling media control
alone keeps track details behind BlueFerry's authenticated Media1 API. A
choice saved through a client or the CLI takes precedence.
"""

SHOW_NOTIFICATION_CONTENT: bool = _env_bool(
    "BLUEFERRY_SHOW_NOTIFICATION_CONTENT", True
)
KEEP_PHONE_AUDIO_ON_PHONE: bool = _env_bool(
    "BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE", True
)
CALLS_ENABLED: bool = _env_opt_in("BLUEFERRY_CALLS_ENABLED")
"""Initial value of the experimental, default-off HFP call control.

A preference saved in settings.json (Qt settings, ``blueferry calls
enable``/``disable``) wins; see ``blueferry.calls.settings``.

When enabled the daemon watches oFono for the iPhone's hands-free modem and
exposes the private ``Calls1`` interface. The WirePlumber phone-audio policy
then keeps the hands-free roles so call audio can reach this computer, while
still stripping ``a2dp_sink`` when ``KEEP_PHONE_AUDIO_ON_PHONE`` is true.
"""
PHONE_BATTERY_NOTIFY: bool = _env_opt_in("BLUEFERRY_PHONE_BATTERY_NOTIFY")
"""Default-off desktop warning when the iPhone's battery runs low.

The level comes from the iPhone's Bluetooth LE battery (``Battery1`` or
the GATT Battery Level), which needs no calls; with ``CALLS_ENABLED`` the
HFP ``battchg`` indicator (20 % steps) fills in when no LE level is known.
"""
PHONE_BATTERY_LOW_PERCENT: int = _env_int(
    "BLUEFERRY_PHONE_BATTERY_LOW_PERCENT", 20, 0, 80
)
"""Warn at or below this level. HFP reports 0-100 % in 20 % steps only."""
NOTIFICATION_TIMEOUT_MS: int = _env_int(
    "BLUEFERRY_NOTIFICATION_TIMEOUT_MS", 8_000, 1_000, 60_000
)


ANCS_ACTION_TIMEOUT_MS: int = _env_int(
    "BLUEFERRY_ANCS_ACTION_TIMEOUT_MS", 30_000, 1_000, 120_000
)
"""Lifetime of ANCS popups that carry action buttons (e.g. a ringing call)."""

MARK_READ_ON_DISMISS: bool = _env_bool("BLUEFERRY_MARK_READ_ON_DISMISS", True)
"""Whether dismissing a message's desktop popup marks it read on the iPhone.

Some notification-center "block" actions (menu bar do-not-disturb toggles,
some panel widgets) dismiss the popup rather than merely hiding it, which
would otherwise mark the message read on the phone without the user ever
seeing it.
"""
OTP_AUTOCOPY: bool = _env_bool("BLUEFERRY_OTP_AUTOCOPY", False)
"""Copy one-time codes from newly received messages to the clipboard.

Off by default: it changes the clipboard without a user action. The code is
never logged or broadcast, and the confirmation popup shows it only when
``SHOW_NOTIFICATION_CONTENT`` is enabled.
"""
OTP_CLEAR_SECONDS: int = _env_int("BLUEFERRY_OTP_CLEAR_SECONDS", 0, 0, 600)
"""Clear a copied code after this many seconds if nothing replaced it (0 = keep)."""
CONTACT_PHOTOS: bool = _env_bool("BLUEFERRY_CONTACT_PHOTOS", False)
"""Opt-in: keep PBAP contact photos and offer them as avatars.

Off by default. Photos enlarge the private contact cache and put more
remote-controlled bytes in front of client image decoders; see
``blueferry.contact_photos`` for the threat model.
"""
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
PROXIMITY_LOCK: bool = _env_bool("BLUEFERRY_PROXIMITY_LOCK", False)
"""Initial opt-in for locking the desktop when the iPhone goes away.

Off by default. This is a lock trigger only, never an unlock or an
authentication factor. A value saved through the D-Bus API (settings.json)
takes precedence; see ``proximity_lock.ProximityLockSettings``.
"""
PROXIMITY_LOCK_GRACE_SEC: int = _env_int(
    "BLUEFERRY_PROXIMITY_LOCK_GRACE_SEC", 60, 10, 3600
)
"""Seconds the iPhone must stay continuously disconnected before locking."""

CALL_HISTORY_ENABLED: bool = _env_bool("BLUEFERRY_CALL_HISTORY_ENABLED", False)
"""Opt-in: pull the iPhone's recent calls over PBAP and retain them locally.

Off by default because it retains who called whom and when. It uses the
existing PBAP session (the iPhone's **Sync Contacts** permission) and the same
local storage policy and retention window as message history.
"""
CALL_HISTORY_INTERVAL_SEC: int = _env_int(
    "BLUEFERRY_CALL_HISTORY_INTERVAL_SEC", 900, 60, 24 * 60 * 60
)
"""Seconds between fallback polls of the missed-calls list.

PBAP has no change events. ANCS call notifications trigger prompt pulls, so
this poll only covers calls ANCS did not report (e.g. notifications off).
"""
MISSED_CALL_NOTIFICATIONS: bool = _env_bool(
    "BLUEFERRY_MISSED_CALL_NOTIFICATIONS", True
)
"""Desktop popups for newly seen missed calls; only with call history enabled."""

TETHER_ENABLED: bool = _env_bool("BLUEFERRY_TETHER_ENABLED", False)
"""Initial opt-in for Bluetooth tethering as a whole.

Off by default. While off, the daemon neither watches nor adopts PAN links,
does not hold back adapter recovery for them, and refuses ``Tether1.Connect``.
A value saved through the D-Bus API (settings.json) takes precedence; see
``tether.TetherSettings``.
"""
TETHER_AUTOCONNECT: bool = _env_bool("BLUEFERRY_TETHER_AUTOCONNECT", False)
"""Initial value for starting tethering automatically once MAP/PBAP are up.

Off by default and effective only while tethering is enabled. An explicit
disconnect pauses automatic attempts until the next explicit connect. A value
saved through the D-Bus API takes precedence.
"""


def _tether_backend() -> str:
    value = os.environ.get("BLUEFERRY_TETHER_BACKEND", "auto").strip().casefold()
    return value if value in {"auto", "networkmanager", "bluez"} else "auto"


TETHER_BACKEND: str = _tether_backend()
"""``auto`` prefers NetworkManager when it is running; ``bluez`` only brings
the PAN link up and leaves DHCP to the user."""

# ---- runtime paths ------------------------------------------------------

_state_home = Path(
    os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
) / "blueferry"

STATE_DIR: Path = _state_home
EVENTS_DB: Path = _state_home / "events.sqlite"
CONTACTS_DB: Path = _state_home / "contacts.sqlite"
CALLS_DB: Path = _state_home / "calls.sqlite"

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

    for path in (EVENTS_DB, CONTACTS_DB, CALLS_DB):
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
