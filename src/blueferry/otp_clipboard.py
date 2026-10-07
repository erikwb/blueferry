"""Put a one-time code on the desktop clipboard from the backend daemon.

The daemon writes the clipboard itself instead of asking a GUI client to do
it: a code should be pasteable even when no BlueFerry window is open, and
passing it to a client would put the code on the session bus. On Wayland a
background process can only own the clipboard through a data-control
protocol, which Qt does not use but ``wl-copy`` (wl-clipboard) does; KWin,
wlroots compositors, and others support it. GNOME/Mutter without
data-control is untested.

The helper runs in the foreground and owns the selection for as long as it
lives. That makes "clear the clipboard only if it still holds the code"
exact without reading the clipboard back: when anything else is copied the
helper exits on its own, so a helper that is still running still owns the
code, and stopping it clears the selection.

The code is written to the helper's stdin, never to argv (argv is readable
by every local user), and the helper gets only an allowlisted environment.
With wl-clipboard 2.3 or newer ``--sensitive`` also offers
``x-kde-passwordManagerHint: secret`` so Klipper and other clipboard
managers keep the code out of their history.

A helper also exits when a clipboard persistence tool (wl-clip-persist,
clipboard managers that keep the selection alive) takes the selection over
right away. The code is then still on the clipboard although the helper is
gone, so the clear timer and backend shutdown read the clipboard back and
clear it only if it still holds exactly the code.

Nothing here blocks the GLib main loop while it runs: the ``--sensitive``
probe and the read-back run on a background worker, helpers are reaped by
a GLib child watch, and stopping a helper escalates to SIGKILL from a timer
instead of waiting. Only backend shutdown reads the clipboard back
synchronously, bounded by a short timeout, because no worker callback is
delivered after the main loop stopped.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import stat

# Only for the Popen type; commands.py spawns processes.
import subprocess  # nosec B404
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from blueferry import commands
from blueferry.errors import CommandError

log = logging.getLogger(__name__)

_WAYLAND_SOCKET = re.compile(r"^wayland-[0-9]+$")
_PROBE_TIMEOUT_S = 2.0
# Reading the clipboard back and clearing it; shorter at shutdown.
_READ_BACK_TIMEOUT_S = 2.0
_SHUTDOWN_READ_BACK_TIMEOUT_S = 0.5
_KILL_AFTER_MS = 1000
_POLL_INTERVAL_MS = 500
_MAX_POLL_INTERVAL_MS = 10_000

# The helper needs to find its display and a temporary directory, nothing
# else from the daemon's environment.
_HELPER_ENV_KEYS = frozenset({
    "PATH", "HOME", "TMPDIR", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY",
    "XAUTHORITY", "LANG",
})

DisplayKind = Literal["wayland", "x11"]
TicketState = Literal["running", "exited", "failed", "superseded"]
Submit = Callable[..., None]


@dataclass(frozen=True, slots=True)
class ClipboardTarget:
    """The display a helper should talk to and the variables it needs."""

    kind: DisplayKind
    tool: str
    executable: str
    env_overrides: tuple[tuple[str, str], ...] = ()


@dataclass(eq=False)
class ClipboardTicket:
    """One started helper; identity distinguishes overlapping copies."""

    tool: str
    process: Any
    returncode: int | None = None
    pidfd: int | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.returncode is None


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
    which: Callable[[str], str | None] | None = None,
    list_sockets: Callable[[str | None], list[str]] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> ClipboardTarget | None:
    """Choose a clipboard helper for the graphical session, or ``None``.

    A user service can start before the desktop exports ``WAYLAND_DISPLAY``
    to the service manager, and its environment never changes afterwards.
    When the variable is missing, a single ``wayland-N`` socket in the
    owner-only runtime directory identifies the session unambiguously.
    Helpers named in ``exclude`` are skipped (used to fall back to X11 when
    wl-copy could not reach the guessed display).

    Helpers are searched in ``environ``'s ``PATH``, the same environment
    the helper is started with, never in the process's own; without a
    ``PATH`` there is no helper.
    """
    search_path = environ.get("PATH", "")

    def lookup(tool: str) -> str | None:
        if which is not None:
            return which(tool)
        return shutil.which(tool, path=search_path) if search_path else None

    sockets_in = list_sockets or _wayland_sockets

    def resolve(tool: str) -> str | None:
        if tool in exclude:
            return None
        found = lookup(tool)
        return os.path.abspath(found) if found else None

    wl_copy = resolve("wl-copy")
    if wl_copy:
        display = environ.get("WAYLAND_DISPLAY", "").strip()
        if display:
            return ClipboardTarget("wayland", "wl-copy", wl_copy)
        sockets = sockets_in(environ.get("XDG_RUNTIME_DIR"))
        if len(sockets) == 1:
            return ClipboardTarget(
                "wayland", "wl-copy", wl_copy, (("WAYLAND_DISPLAY", sockets[0]),)
            )
    if environ.get("DISPLAY", "").strip():
        for tool in ("xclip", "xsel"):
            executable = resolve(tool)
            if executable:
                return ClipboardTarget("x11", tool, executable)
    return None


def x11_failure_hint(environ: Mapping[str, str]) -> str | None:
    """Explain an X11 helper failure the shipped systemd unit can cause.

    ``PrivateTmp=true`` hides the host's ``/tmp``. X clients still reach the
    server through its abstract socket (Xorg, and Xwayland under KWin,
    Mutter and wlroots, listen on one), but an ``XAUTHORITY`` file under
    ``/tmp`` becomes invisible. Content-free: only says which case applies.
    """
    xauthority = environ.get("XAUTHORITY", "")
    # Only names the location; nothing is created or written there.
    in_tmp = Path(xauthority).parts[:2] == ("/", "tmp") and len(Path(xauthority).parts) > 2
    if in_tmp and not os.path.exists(xauthority):
        return (
            "XAUTHORITY points into /tmp, which the service's PrivateTmp hides; "
            "a display manager that keeps it under $XDG_RUNTIME_DIR avoids this"
        )
    return None


def helper_environment(environ: Mapping[str, str], target: ClipboardTarget) -> dict[str, str]:
    """Return the allowlisted environment for a clipboard helper."""
    env = {
        key: value
        for key, value in environ.items()
        if key in _HELPER_ENV_KEYS or key.startswith("LC_")
    }
    env.update(target.env_overrides)
    return env


def copy_argv(target: ClipboardTarget, *, sensitive: bool) -> tuple[str, ...]:
    """Build the helper argv; the code itself always goes to stdin.

    Every helper stays in the foreground so its lifetime equals ownership of
    the selection. ``--paste-once`` is deliberately not used: clipboard
    managers read each new selection immediately and would consume the
    single paste. An explicit ``--type text/plain`` still offers the usual
    text aliases and spares wl-copy from inferring a type (which can spawn
    ``xdg-mime``).
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
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Return whether this wl-copy offers ``--sensitive`` (wl-clipboard 2.3+).

    This starts ``wl-copy --help``; the daemon runs it on a worker thread.
    """
    runner = run or commands.run_command
    try:
        result = runner([executable, "--help"], timeout=_PROBE_TIMEOUT_S, check=False)
    except CommandError:
        return False
    return "--sensitive" in f"{result.stdout}\n{result.stderr}"


def paste_argv(target: ClipboardTarget) -> tuple[str, ...] | None:
    """Argv that prints the clipboard's text, or ``None`` if unavailable.

    ``wl-paste`` ships with ``wl-copy`` and is looked up next to it.
    """
    if target.tool == "wl-copy":
        wl_paste = os.path.join(os.path.dirname(target.executable), "wl-paste")
        if not os.access(wl_paste, os.X_OK):
            return None
        return (wl_paste, "--no-newline", "--type", "text/plain")
    if target.tool == "xclip":
        return (target.executable, "-selection", "clipboard", "-o")
    if target.tool == "xsel":
        return (target.executable, "--clipboard", "--output")
    return None


def clear_argv(target: ClipboardTarget) -> tuple[str, ...]:
    """Argv that empties the clipboard."""
    if target.tool == "wl-copy":
        return (target.executable, "--clear")
    if target.tool == "xsel":
        return (target.executable, "--clipboard", "--clear")
    if target.tool == "xclip":
        # xclip has no clear option: it takes the selection with empty input
        # (stdin is /dev/null) and serves it from a background child.
        return (target.executable, "-selection", "clipboard")
    raise ValueError(f"unsupported clipboard tool {target.tool!r}")


def clear_if_unchanged(
    target: ClipboardTarget,
    env: Mapping[str, str],
    code: str,
    *,
    timeout: float = _READ_BACK_TIMEOUT_S,
    read: Callable[..., bytes | None] | None = None,
    run: Callable[..., bool] | None = None,
) -> bool:
    """Clear the clipboard if it still holds exactly ``code``.

    Blocking: runs a paste helper and possibly a clear helper. Only a short
    prefix of the clipboard is read and it is only compared, never kept.
    """
    reader = read or commands.read_command_output
    runner = run or commands.run_quiet
    argv = paste_argv(target)
    if argv is None:
        return False
    data = reader(argv, timeout=timeout, limit=len(code) + 2, env=env)
    if data is None:
        return False
    if data.endswith(b"\n"):
        data = data[:-1]
    if data != code.encode("utf-8"):
        return False
    return bool(runner(clear_argv(target), timeout=timeout, env=env))


def _exit_code(status: int) -> int:
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        return -1


def send_signal(ticket: ClipboardTicket, signum: int) -> None:
    """Signal a helper that has not been reaped yet.

    A pidfd cannot hit a recycled PID; plain ``kill`` is the fallback where
    pidfds are unavailable.
    """
    if not ticket.running:
        return
    try:
        if ticket.pidfd is not None:
            signal.pidfd_send_signal(ticket.pidfd, signum)
        else:
            os.kill(int(ticket.process.pid), signum)
    except ProcessLookupError:
        pass


def _open_pidfd(pid: int) -> int | None:
    try:
        return os.pidfd_open(pid)
    except (AttributeError, OSError):
        return None


def _glib_watch_child(pid: int, callback: Callable[[int, int], None]) -> int:
    from gi.repository import GLib

    return GLib.child_watch_add(
        GLib.PRIORITY_DEFAULT, pid, lambda child, status: callback(child, status)
    )


class ClipboardWriter:
    """Owns at most one clipboard helper process at a time."""

    def __init__(
        self,
        *,
        clear_after_s: int = 0,
        environ: Mapping[str, str] | None = None,
        find: Callable[..., ClipboardTarget | None] | None = None,
        spawn: Callable[..., Any] | None = None,
        probe_sensitive: Callable[[str], bool] | None = None,
        submit_probe: Submit | None = None,
        schedule_ms: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], object] | None = None,
        watch_child: Callable[[int, Callable[[int, int], None]], object] | None = None,
        signal_helper: Callable[[ClipboardTicket, int], None] | None = None,
        open_pidfd: Callable[[int], int | None] = _open_pidfd,
        clear_unchanged: Callable[..., bool] = clear_if_unchanged,
    ) -> None:
        self._worker = None
        if schedule_ms is None or cancel is None:
            from gi.repository import GLib

            schedule_ms = schedule_ms or GLib.timeout_add
            cancel = cancel or GLib.source_remove
        if submit_probe is None:
            from blueferry.background_worker import BackgroundWorker

            self._worker = BackgroundWorker("otp-clipboard-probe", maximum=2)
            submit_probe = self._worker.submit
        self.clear_after_s = max(0, int(clear_after_s))
        self._environ = environ
        self._find = find or find_target
        self._spawn = spawn or commands.spawn_command
        self._probe_sensitive = probe_sensitive or supports_sensitive_hint
        self._submit_probe = submit_probe
        self._schedule_ms = schedule_ms
        self._cancel = cancel
        self._watch_child = watch_child or _glib_watch_child
        self._signal = signal_helper or send_signal
        self._open_pidfd = open_pidfd
        self._clear_unchanged = clear_unchanged
        # What the clear timer needs if the helper hands the code to another
        # clipboard owner: (ticket, target, helper env, code). Held only
        # while a clear timer is pending.
        self._clear_state: tuple[ClipboardTicket, ClipboardTarget, dict[str, str], str] | None = (
            None
        )
        # executable -> True/False once probed; missing while unknown.
        self._sensitive: dict[str, bool] = {}
        self._probing: set[str] = set()
        self._owner: ClipboardTicket | None = None
        # Stopped helpers stay referenced until reaped, so Popen never reaps
        # them behind the GLib child watch.
        self._stopping: dict[int, ClipboardTicket] = {}
        self._clear_id: int | None = None
        self._warned_missing = False
        self._warned_no_fallback = False
        self._warned_insensitive = False

    def _current_environ(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    # ---- sensitive-hint probe ------------------------------------------

    def start_probe(self) -> None:
        """Probe wl-copy for ``--sensitive`` in the background, once."""
        target = self._find(self._current_environ())
        if target is not None and target.tool == "wl-copy":
            self._probe(target.executable)

    def _probe(self, executable: str) -> None:
        if executable in self._sensitive or executable in self._probing:
            return
        self._probing.add(executable)
        try:
            self._submit_probe(
                lambda: self._probe_sensitive(executable),
                on_success=lambda result: self._probed(executable, bool(result)),
                on_error=lambda _error: self._probed(executable, False),
            )
        except RuntimeError:
            self._probing.discard(executable)
            log.debug("could not queue the wl-copy capability probe")

    def _probed(self, executable: str, sensitive: bool) -> None:
        self._probing.discard(executable)
        self._sensitive[executable] = sensitive
        if not sensitive:
            self._warn_insensitive("wl-copy")

    def _warn_insensitive(self, tool: str) -> None:
        if self._warned_insensitive:
            return
        self._warned_insensitive = True
        log.warning(
            "%s cannot mark clipboard data as sensitive; clipboard managers "
            "may keep copied codes in their history (wl-clipboard 2.3+ can)",
            tool,
        )

    # ---- copying ---------------------------------------------------------

    def copy(self, code: str, *, exclude: frozenset[str] = frozenset()) -> ClipboardTicket | None:
        """Copy ``code``; return a ticket for the helper, or ``None``."""
        environ = self._current_environ()
        target = self._find(environ, exclude=exclude)
        if target is None:
            if exclude:
                # A fallback attempt: wl-copy exists but failed, so "install
                # wl-clipboard" would be misleading.
                if not self._warned_no_fallback:
                    log.warning("no X11 fallback helper for this session")
                    self._warned_no_fallback = True
            elif not self._warned_missing:
                log.warning(
                    "one-time code not copied: no clipboard helper for this "
                    "session (install wl-clipboard, or xclip/xsel on X11)"
                )
                self._warned_missing = True
            return None
        sensitive = False
        if target.tool == "wl-copy":
            known = self._sensitive.get(target.executable)
            if known is None:
                # Not probed yet: copy now without the hint rather than wait.
                self._probe(target.executable)
            sensitive = bool(known)
        else:
            self._warn_insensitive(target.tool)
        self.release()
        env = helper_environment(environ, target)
        try:
            process = self._spawn(
                copy_argv(target, sensitive=sensitive),
                stdin_text=code,
                env=env,
            )
        except (CommandError, ValueError) as error:
            log.warning("clipboard helper %s failed: %s", target.tool, error)
            return None
        ticket = ClipboardTicket(target.tool, process, pidfd=self._open_pidfd(process.pid))
        self._owner = ticket
        try:
            self._watch_child(process.pid, lambda _pid, status: self._exited(ticket, status))
        except Exception:
            # Without a GLib watch nobody else reaps this child, so polling
            # through Popen cannot race; it keeps the ticket state honest.
            log.debug("could not watch the clipboard helper; polling it", exc_info=True)
            self._schedule_poll(ticket, _POLL_INTERVAL_MS)
        if self.clear_after_s:
            self._clear_state = (ticket, target, env, code)
            self._clear_id = self._schedule_ms(
                self.clear_after_s * 1000, lambda: self._expire(ticket)
            )
        return ticket

    def failure_hint(self, ticket: ClipboardTicket) -> str | None:
        """A content-free reason why ``ticket``'s helper may have failed."""
        if ticket.tool in ("xclip", "xsel"):
            return x11_failure_hint(self._current_environ())
        return None

    def state(self, ticket: ClipboardTicket) -> TicketState:
        """Describe a ticket; a newer copy or a release supersedes it."""
        if ticket is not self._owner:
            return "superseded"
        if ticket.running:
            return "running"
        return "exited" if ticket.returncode == 0 else "failed"

    def _schedule_poll(self, ticket: ClipboardTicket, delay_ms: int) -> None:
        self._schedule_ms(delay_ms, lambda: self._poll(ticket, delay_ms))

    def _poll(self, ticket: ClipboardTicket, delay_ms: int) -> bool:
        """Fallback reaper when no child watch could be installed.

        Polls with a doubling interval (up to ten seconds) only while the
        ticket still matters: it owns the clipboard, or a release is waiting
        for it to exit. Each poll is a one-shot timer, so a ticket that no
        longer matters leaves no timer behind.
        """
        if not ticket.running:
            return False
        if ticket is not self._owner and id(ticket) not in self._stopping:
            return False
        returncode = ticket.process.poll()
        if returncode is not None:
            self._record_exit(ticket, int(returncode))
            return False
        self._schedule_poll(ticket, min(delay_ms * 2, _MAX_POLL_INTERVAL_MS))
        return False

    def _exited(self, ticket: ClipboardTicket, status: int) -> None:
        # GLib reaped the child; stop Popen from ever waiting on it again.
        self._record_exit(ticket, _exit_code(status))

    def _record_exit(self, ticket: ClipboardTicket, returncode: int) -> None:
        ticket.returncode = returncode
        ticket.process.returncode = returncode
        if ticket.pidfd is not None:
            try:
                os.close(ticket.pidfd)
            except OSError:
                pass
            ticket.pidfd = None
        self._stopping.pop(id(ticket), None)

    def _expire(self, ticket: ClipboardTicket) -> bool:
        self._clear_id = None
        if ticket is not self._owner:
            return False
        if ticket.running:
            log.info("cleared the one-time code from the clipboard")
            self.release()
            return False
        handed_off = self._take_handed_off(ticket)
        self.release()
        if handed_off is None:
            return False
        target, env, code = handed_off
        try:
            self._submit_probe(
                lambda: self._clear_unchanged(target, env, code),
                on_success=self._cleared_after_handoff,
                on_error=lambda error: log.debug(
                    "could not read the clipboard back: %s", type(error).__name__
                ),
            )
        except RuntimeError:
            log.debug("could not queue the clipboard read-back")
        return False

    def _take_handed_off(
        self, ticket: ClipboardTicket
    ) -> tuple[ClipboardTarget, dict[str, str], str] | None:
        """Return what a read-back needs when the helper exited cleanly.

        A clean exit means another program took the selection: either the
        user copied something else, or a persistence tool now holds the
        code. Only reading the clipboard back tells the two apart.
        """
        state, self._clear_state = self._clear_state, None
        if state is None or state[0] is not ticket or ticket.returncode != 0:
            return None
        return state[1], state[2], state[3]

    @staticmethod
    def _cleared_after_handoff(cleared: object) -> None:
        if cleared:
            log.info("cleared the one-time code that another clipboard owner held")

    def release(self) -> None:
        """Stop the current helper; a helper still running clears the code.

        The SIGKILL escalation timer is deliberately not tracked or
        cancelled: it only signals a helper that is still running, and at
        daemon shutdown the service manager stops whatever is left in the
        service's cgroup anyway.
        """
        if self._clear_id is not None:
            try:
                self._cancel(self._clear_id)
            except Exception:
                log.debug("could not cancel the clipboard clear timer", exc_info=True)
            self._clear_id = None
        self._clear_state = None
        ticket, self._owner = self._owner, None
        if ticket is None or not ticket.running:
            return
        self._stopping[id(ticket)] = ticket
        self._signal(ticket, signal.SIGTERM)
        self._schedule_ms(_KILL_AFTER_MS, lambda: self._kill_if_running(ticket))

    def _kill_if_running(self, ticket: ClipboardTicket) -> bool:
        if ticket.running:
            self._signal(ticket, signal.SIGKILL)
        return False

    def close(self) -> None:
        """Release the clipboard and stop the probe worker.

        A code still waiting for its clear timer is removed now: a helper
        that still owns it is stopped, and a code handed to another
        clipboard owner is read back and cleared synchronously (bounded by
        a short timeout), since no worker reply arrives after shutdown.
        """
        owner = self._owner
        handed_off = (
            self._take_handed_off(owner)
            if owner is not None and self._clear_id is not None
            else None
        )
        self.release()
        if handed_off is not None:
            target, env, code = handed_off
            try:
                self._cleared_after_handoff(
                    self._clear_unchanged(
                        target, env, code, timeout=_SHUTDOWN_READ_BACK_TIMEOUT_S
                    )
                )
            except Exception as error:
                log.debug("could not read the clipboard back: %s", type(error).__name__)
        if self._worker is not None:
            self._worker.close()
            self._worker = None
