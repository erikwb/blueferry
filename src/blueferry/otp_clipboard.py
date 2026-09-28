"""Put a one-time code on the desktop clipboard from the backend daemon.

The daemon writes the clipboard itself instead of asking a GUI client to do
it: a code should be pasteable even when no BlueFerry window is open, and
passing it to a client would put the code on the session bus. On Wayland a
background process can only own the clipboard through a data-control
protocol, which Qt does not use but ``wl-copy`` (wl-clipboard) does; KWin,
wlroots compositors, and others support it.

The helper runs in the foreground and owns the selection for as long as it
lives. That makes "clear the clipboard only if it still holds the code"
exact without reading the clipboard back: when anything else is copied the
helper exits on its own, so a helper that is still running still owns the
code, and stopping it clears the selection.

The code is written to the helper's stdin, never to argv (argv is readable
by every local user). With wl-clipboard 2.3 or newer ``--sensitive`` also
offers ``x-kde-passwordManagerHint: secret`` so Klipper and other clipboard
managers keep the code out of their history.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat

# Only for the Popen type and TimeoutExpired; commands.py spawns processes.
import subprocess  # nosec B404
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from blueferry import commands
from blueferry.errors import CommandError

log = logging.getLogger(__name__)

_WAYLAND_SOCKET = re.compile(r"^wayland-[0-9]+$")
_PROBE_TIMEOUT_S = 2.0
_STOP_TIMEOUT_S = 1.0

DisplayKind = Literal["wayland", "x11"]


@dataclass(frozen=True, slots=True)
class ClipboardTarget:
    """The display a helper should talk to and the variables it needs."""

    kind: DisplayKind
    tool: str
    executable: str
    env_overrides: tuple[tuple[str, str], ...] = ()


def _wayland_sockets(runtime_dir: str | None) -> list[str]:
    if not runtime_dir:
        return []
    try:
        names = sorted(os.listdir(runtime_dir))
    except OSError:
        return []
    sockets = []
    for name in names:
        if not _WAYLAND_SOCKET.fullmatch(name):
            continue
        try:
            mode = os.lstat(Path(runtime_dir) / name).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            sockets.append(name)
    return sockets


def find_target(
    environ: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    list_sockets: Callable[[str | None], list[str]] = _wayland_sockets,
) -> ClipboardTarget | None:
    """Choose a clipboard helper for the graphical session, or ``None``.

    A user service can start before the desktop exports ``WAYLAND_DISPLAY``
    to the service manager, and its environment never changes afterwards.
    When the variable is missing, a single ``wayland-N`` socket in the
    owner-only runtime directory identifies the session unambiguously.
    """
    wl_copy = which("wl-copy")
    if wl_copy:
        display = environ.get("WAYLAND_DISPLAY", "").strip()
        if display:
            return ClipboardTarget("wayland", "wl-copy", wl_copy)
        sockets = list_sockets(environ.get("XDG_RUNTIME_DIR"))
        if len(sockets) == 1:
            return ClipboardTarget(
                "wayland", "wl-copy", wl_copy, (("WAYLAND_DISPLAY", sockets[0]),)
            )
    if environ.get("DISPLAY", "").strip():
        for tool in ("xclip", "xsel"):
            executable = which(tool)
            if executable:
                return ClipboardTarget("x11", tool, executable)
    return None


def copy_argv(target: ClipboardTarget, *, sensitive: bool) -> tuple[str, ...]:
    """Build the helper argv; the code itself always goes to stdin.

    Every helper stays in the foreground so its lifetime equals ownership of
    the selection. ``--paste-once`` is deliberately not used: clipboard
    managers read each new selection immediately and would consume the
    single paste.
    """
    if target.tool == "wl-copy":
        argv = [target.executable, "--foreground", "--type", "text/plain"]
        if sensitive:
            argv.append("--sensitive")
        return tuple(argv)
    if target.tool == "xclip":
        # -quiet keeps xclip in the foreground; it exits when the selection
        # is taken by another client.
        return (target.executable, "-selection", "clipboard", "-quiet")
    if target.tool == "xsel":
        return (target.executable, "--clipboard", "--input", "--nodetach")
    raise ValueError(f"unsupported clipboard tool {target.tool!r}")


def supports_sensitive_hint(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = commands.run_command,
) -> bool:
    """Return whether this wl-copy offers ``--sensitive`` (wl-clipboard 2.3+)."""
    try:
        result = run([executable, "--help"], timeout=_PROBE_TIMEOUT_S, check=False)
    except CommandError:
        return False
    return "--sensitive" in f"{result.stdout}\n{result.stderr}"


class ClipboardWriter:
    """Owns at most one clipboard helper process at a time."""

    def __init__(
        self,
        *,
        clear_after_s: int = 0,
        environ: Mapping[str, str] | None = None,
        find: Callable[[Mapping[str, str]], ClipboardTarget | None] = find_target,
        spawn: Callable[..., subprocess.Popen[bytes]] = commands.spawn_command,
        probe_sensitive: Callable[[str], bool] = supports_sensitive_hint,
        schedule: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], object] | None = None,
    ) -> None:
        if schedule is None or cancel is None:
            from gi.repository import GLib

            schedule = schedule or GLib.timeout_add_seconds
            cancel = cancel or GLib.source_remove
        self.clear_after_s = max(0, int(clear_after_s))
        self._environ = environ
        self._find = find
        self._spawn = spawn
        self._probe_sensitive = probe_sensitive
        self._schedule = schedule
        self._cancel = cancel
        self._sensitive: dict[str, bool] = {}
        self._owner: subprocess.Popen[bytes] | None = None
        self._clear_id: int | None = None
        self._warned_missing = False
        self._warned_insensitive = False

    def copy(self, code: str) -> str | None:
        """Copy ``code``; return the helper name, or ``None`` on failure."""
        environ = self._environ if self._environ is not None else os.environ
        target = self._find(environ)
        if target is None:
            if not self._warned_missing:
                log.warning(
                    "one-time code not copied: no clipboard helper for this "
                    "session (install wl-clipboard, or xclip/xsel on X11)"
                )
                self._warned_missing = True
            return None
        sensitive = False
        if target.tool == "wl-copy":
            if target.executable not in self._sensitive:
                self._sensitive[target.executable] = self._probe_sensitive(target.executable)
            sensitive = self._sensitive[target.executable]
        if not sensitive and not self._warned_insensitive:
            log.warning(
                "%s cannot mark clipboard data as sensitive; clipboard managers "
                "may keep copied codes in their history (wl-clipboard 2.3+ can)",
                target.tool,
            )
            self._warned_insensitive = True
        self.release()
        env = dict(environ)
        env.update(target.env_overrides)
        try:
            self._owner = self._spawn(
                copy_argv(target, sensitive=sensitive), stdin_text=code, env=env
            )
        except (CommandError, ValueError) as error:
            log.warning("clipboard helper %s failed: %s", target.tool, error)
            return None
        if self.clear_after_s:
            owner = self._owner
            self._clear_id = self._schedule(
                self.clear_after_s, lambda: self._expire(owner)
            )
        return target.tool

    def helper_failed(self) -> bool:
        """True when the current helper already exited with an error."""
        owner = self._owner
        if owner is None:
            return True
        returncode = owner.poll()
        return returncode is not None and returncode != 0

    def _expire(self, owner: subprocess.Popen[bytes]) -> bool:
        self._clear_id = None
        if owner is self._owner:
            if owner.poll() is None:
                log.info("cleared the one-time code from the clipboard")
            self.release()
        return False

    def release(self) -> None:
        """Stop the current helper; a helper still running clears the code."""
        if self._clear_id is not None:
            try:
                self._cancel(self._clear_id)
            except Exception:
                log.debug("could not cancel the clipboard clear timer", exc_info=True)
            self._clear_id = None
        owner, self._owner = self._owner, None
        if owner is None:
            return
        if owner.poll() is None:
            owner.terminate()
            try:
                owner.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait()
