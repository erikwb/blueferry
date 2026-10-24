"""A plugin's standard log file (PLUGINS.md, plugin API 1.4), for every client.

The plugin writes ``$XDG_STATE_HOME/blueferry/plugins/<Id>.log`` itself
(:mod:`blueferry.plugin_api.logs`). A bus-activated plugin may not share the
client's ``XDG_STATE_HOME``, so the clients look at both that and
``~/.local/state``. Only a regular file of this user is read, never through
a symlink, and at most the last 256 KiB. The plugin already masks secrets;
the clients still show the text as plain text only.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from blueferry.plugin_api.logs import candidate_paths, valid_plugin_id

MAX_TAIL_BYTES = 256 * 1024
DEFAULT_LINES = 200


def find_log(plugin_id: str) -> Path | None:
    """The newest existing log file of ``plugin_id``, or None."""
    if not valid_plugin_id(plugin_id):
        return None
    found: list[tuple[float, Path]] = []
    for path in candidate_paths(plugin_id):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            found.append((info.st_mtime, path))
    return max(found)[1] if found else None


def read_tail(path: Path, lines: int = DEFAULT_LINES) -> str:
    """The last ``lines`` lines of ``path`` (at most 256 KiB); OSError if
    it is not a regular file of this user."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("not a log file of this user")
        start = max(0, info.st_size - MAX_TAIL_BYTES)
        stream.seek(start)
        data = stream.read(MAX_TAIL_BYTES)
    text = data.decode("utf-8", "replace")
    parts = text.splitlines()
    if start > 0 and parts:
        parts = parts[1:]  # the first line was cut in the middle
    return "\n".join(parts[-max(1, lines):])


def tail(plugin_id: str, lines: int = DEFAULT_LINES) -> tuple[Path | None, str]:
    """(path, text) of the plugin's log; (None, "") when it has none yet."""
    path = find_log(plugin_id)
    if path is None:
        return None, ""
    try:
        return path, read_tail(path, lines)
    except OSError:
        return None, ""
