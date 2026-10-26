"""The standard log file of a plugin (ApiVersion 1.4).

A D-Bus activated plugin inherits the bus daemon's stdout and stderr, which
usually point at a virtual console or nowhere, so its log lines were lost.
:func:`setup_logging` (called by :func:`blueferry.plugin_api.service.run`
for every plugin) adds a rotating file at

    $XDG_STATE_HOME/blueferry/plugins/<Id>.log   (~/.local/state/… without it)

owner-only (directory 0700, files 0600, no symlinks followed), at most
512 KiB plus two older generations. Every line passes :func:`redact`, which
masks values of keys such as ``token``, ``password``, ``pin`` or
``Authorization`` that slipped into a message; plugins still must not log
secrets or personal data on purpose. Uncaught exceptions (main and worker
threads) and Python warnings land in the same file.

The clients read it with :func:`candidate_paths` ("Show log" in the Qt
settings, ``blueferry plugins log ID``). The level is
INFO; ``BLUEFERRY_PLUGIN_LOG_LEVEL=DEBUG`` in the plugin's environment
raises it.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import stat
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

MAX_BYTES = 512 * 1024
BACKUPS = 2
LEVEL_VARIABLE = "BLUEFERRY_PLUGIN_LOG_LEVEL"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}$")
_SECRET_KEYS = (
    r"pin|token|access[_-]?token|refresh[_-]?token|password|passwd|app[_-]?password|"
    r"secret|client[_-]?secret|api[_-]?key|apikey|session[_-]?id|sessionid|cookie"
)
_PATTERNS = (
    # Authorization: Bearer abc / Basic abc
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\"?)(\w+\s+)?[^\s\"',;]+"), r"\1\2\3***"),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2***"),
    # token=abc, "password": "abc", pin: 1234, ?sessionId=abc&…
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])(" + _SECRET_KEYS + r")([\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+",
    ), r"\1\2***"),
)


def redact(text: str) -> str:
    """``text`` with the values of secret-looking keys replaced by ``***``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def valid_plugin_id(plugin_id: object) -> bool:
    return isinstance(plugin_id, str) and _ID.fullmatch(plugin_id) is not None and (
        ".." not in plugin_id
    )


def state_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    value = env.get("XDG_STATE_HOME", "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(os.path.expanduser("~")) / ".local" / "state"


def log_directory(environ: Mapping[str, str] | None = None) -> Path:
    return state_home(environ) / "blueferry" / "plugins"


def log_path(plugin_id: str, environ: Mapping[str, str] | None = None) -> Path:
    if not valid_plugin_id(plugin_id):
        raise ValueError("not a plugin id")
    return log_directory(environ) / f"{plugin_id}.log"


def candidate_paths(plugin_id: str, environ: Mapping[str, str] | None = None) -> list[Path]:
    """Where a client looks: its own ``XDG_STATE_HOME`` and the default.

    A bus-activated plugin gets the bus daemon's environment, which may lack
    the client's ``XDG_STATE_HOME``.
    """
    paths = [log_path(plugin_id, environ), log_path(plugin_id, {})]
    return list(dict.fromkeys(paths))


class RedactingFormatter(logging.Formatter):
    """Formats like ``logging.Formatter`` (tracebacks included), then redacts."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler whose files are 0600 and never a symlink."""

    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("the log file is not a regular file of this user")
            os.fchmod(descriptor, 0o600)
        except OSError:
            os.close(descriptor)
            raise
        return os.fdopen(descriptor, "a", encoding="utf-8", errors="replace")


def _private_directory(path: Path, base: Path) -> None:
    """Create ``path`` below ``base`` one level at a time: every created or
    existing level must be a real directory of this user (no symlink), and
    the levels this module owns (``blueferry``, ``plugins``) are 0700."""
    base.mkdir(parents=True, exist_ok=True)
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("the log directory is not a directory of this user")
        if info.st_mode & 0o077:
            os.chmod(current, 0o700)


def _level(environ: Mapping[str, str]) -> int:
    name = environ.get(LEVEL_VARIABLE, "").strip().upper()
    value = logging.getLevelName(name) if name else logging.INFO
    return value if isinstance(value, int) else logging.INFO


def _install_hooks() -> None:
    if getattr(sys.excepthook, "_blueferry_plugin_log", False):
        return
    previous = sys.excepthook
    previous_thread = threading.excepthook

    def excepthook(kind, value, traceback) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            logging.getLogger("blueferry.plugin").critical(
                "uncaught exception", exc_info=(kind, value, traceback),
            )
        previous(kind, value, traceback)

    def thread_excepthook(args) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            logging.getLogger("blueferry.plugin").error(
                "uncaught exception in thread %s", name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        previous_thread(args)

    excepthook._blueferry_plugin_log = True  # type: ignore[attr-defined]
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def setup_logging(
    plugin_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    max_bytes: int = MAX_BYTES,
    backups: int = BACKUPS,
) -> Path | None:
    """Log this process to the plugin's file; idempotent.

    Returns the path, or None when the file cannot be used (the reason goes
    to stderr; a plugin never fails to start over its log).
    """
    env = os.environ if environ is None else environ
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "blueferry_plugin_id", None) == plugin_id:
            return Path(handler.baseFilename)  # type: ignore[attr-defined]
    try:
        path = log_path(plugin_id, env)
        _private_directory(path.parent, state_home(env))
        handler = PrivateRotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        print(f"blueferry plugin log unavailable: {error}", file=sys.stderr)
        return None
    handler.blueferry_plugin_id = plugin_id  # type: ignore[attr-defined]
    handler.setFormatter(RedactingFormatter(FORMAT))
    level = _level(env)
    handler.setLevel(level)
    if root.level == logging.NOTSET or root.level > level:
        # Let the file see INFO without making the plugin's other handlers
        # (stderr) chattier than before: they keep the old threshold.
        for other in root.handlers:
            if other.level == logging.NOTSET and root.level != logging.NOTSET:
                other.setLevel(root.level)
        root.setLevel(level)
    root.addHandler(handler)
    logging.captureWarnings(True)
    _install_hooks()
    return path


__all__ = [
    "BACKUPS",
    "LEVEL_VARIABLE",
    "MAX_BYTES",
    "PrivateRotatingFileHandler",
    "RedactingFormatter",
    "candidate_paths",
    "log_directory",
    "log_path",
    "redact",
    "setup_logging",
    "state_home",
    "valid_plugin_id",
]
