"""Launchers for companion tools that sit next to BlueFerry on the desktop.

These are client-side conveniences, not part of the daemon or its D-Bus API:

``mirror``
    UxPlay, an AirPlay receiver, for the iPhone's Screen Mirroring. A
    ``uxplay.desktop`` entry (for your own UxPlay options) is preferred;
    otherwise ``uxplay`` from PATH is started with this computer's host name.
``send``
    LocalSend (the Flathub app, or ``localsend`` from PATH). Hidden while an
    enabled plugin declares ``ReplacesTools=localsend`` (the LocalSend
    plugin answers on the same port, so the app would only get in its way).
``photos`` / ``eject``
    The iPhone's camera roll over USB through ifuse, mounted below
    ``$XDG_RUNTIME_DIR/blueferry`` and opened in the file manager.

Every tool only shows up as usable when it is installed. The process
launchers and the tool lookup are injected through :class:`System`, so the
rules are testable without starting anything. Functions marked *blocking*
run external programs with a timeout and belong on a worker thread or in the
CLI, never on a UI thread. Nothing here logs or returns device identifiers.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from blueferry import desktop_open
from blueferry.i18n import _
from blueferry.private_files import (
    atomic_write_private_text,
    ensure_private_directory,
    read_private_text,
    runtime_private_directory,
)

MIRROR = "mirror"
SEND = "send"
PHOTOS = "photos"
EJECT = "eject"
PAIR = "pair"
ACTIONS = (MIRROR, SEND, PHOTOS, EJECT, PAIR)
# Actions that run external programs and therefore need a worker thread.
BLOCKING_ACTIONS = frozenset({PHOTOS, EJECT, PAIR})

UXPLAY = "uxplay"
UXPLAY_DESKTOP_ID = "uxplay.desktop"
LOCALSEND = "localsend"
LOCALSEND_DESKTOP_ID = "org.localsend.localsend_app.desktop"
IFUSE = "ifuse"
IDEVICE_ID = "idevice_id"
IDEVICEPAIR = "idevicepair"
FUSERMOUNT = ("fusermount3", "fusermount")

MIRROR_PID_FILE = "uxplay.pid"
PHOTOS_DIR = "iphone-photos"

PROBE_TIMEOUT_SEC = 5.0
VALIDATE_TIMEOUT_SEC = 10.0
PAIR_TIMEOUT_SEC = 30.0
MOUNT_TIMEOUT_SEC = 20.0
UNMOUNT_TIMEOUT_SEC = 10.0
_OUTPUT_LIMIT = 16 * 1024


class Launcher(Protocol):
    def launch(self) -> int | None:
        """Start the program; return its process id when it is known."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """*Blocking.* Run ``argv`` with a timeout and collect stdout+stderr.

    Output goes to an unlinked temporary file rather than a pipe: ifuse
    daemonizes after mounting, and a pipe its background half inherits would
    keep ``communicate()`` waiting until the timeout.
    """
    with tempfile.TemporaryFile() as output:
        try:
            # Fixed argv lists from the tool names above, never a shell.
            completed = subprocess.run(  # nosec B603
                list(argv), stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(-1, "", timed_out=True)
        except OSError as error:
            return CommandResult(-1, str(error))
        output.seek(0)
        text = output.read(_OUTPUT_LIMIT).decode("utf-8", "replace")
    return CommandResult(completed.returncode, text)


class _GioLauncher:
    def __init__(self, info: object) -> None:
        self._info = info

    def launch(self) -> int | None:
        from gi.repository import GLib

        pids: list[int] = []
        # Without DO_NOT_REAP_CHILD GLib double-forks: the program is not our
        # child, so it never lingers as a zombie of a long-running client.
        self._info.launch_uris_as_manager(  # type: ignore[attr-defined]
            [], None, GLib.SpawnFlags.SEARCH_PATH, None, None,
            lambda _info, pid, *_data: pids.append(int(pid)), None,
        )
        return pids[0] if pids else None


def _desktop_info_class() -> object:
    try:
        from gi.repository import GioUnix

        return GioUnix.DesktopAppInfo
    except ImportError:
        from gi.repository import Gio

        return Gio.DesktopAppInfo


def gio_desktop_app(desktop_id: str) -> Launcher | None:
    try:
        info = _desktop_info_class().new(desktop_id)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return None
    return _GioLauncher(info) if info is not None else None


def gio_command_app(argv: Sequence[str]) -> Launcher:
    from gi.repository import Gio, GLib

    commandline = " ".join(GLib.shell_quote(part) for part in argv)
    info = Gio.AppInfo.create_from_commandline(
        commandline, os.path.basename(argv[0]), Gio.AppInfoCreateFlags.NONE,
    )
    return _GioLauncher(info)


gio_open_uri = desktop_open.open_uri


def process_argv(pid: int) -> list[str] | None:
    """The command line of a live, non-zombie process; None when it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    state = stat.rpartition(")")[2].split()[:1]
    if state in (["Z"], ["X"]):
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def replaced_by_plugins() -> frozenset[str]:
    """Tool names (``localsend``, ``uxplay``) an enabled plugin replaces
    (manifest key ``ReplacesTools``). *Blocking*: reads the manifests, starts
    nothing; any problem counts as "nothing replaced"."""
    try:
        from blueferry import __version__
        from blueferry.plugin_api.manifest import discover
        from blueferry.plugin_prefs import disabled_plugins

        found = discover(blueferry_version=__version__)
        disabled = disabled_plugins()
    except Exception:  # a broken plugin setup must not hide the tools
        return frozenset()
    return frozenset(
        tool for plugin in found.plugins if plugin.id not in disabled
        for tool in plugin.replaces_tools
    )




def pidfd_terminate(pid: int, still_ours: Callable[[int], bool]) -> None:
    """SIGTERM ``pid`` if it is still ours; ProcessLookupError when it is gone.

    A pidfd pins the process while ``still_ours`` checks it again, so a pid
    that was reused by another program in between is never signalled.
    Without pidfd support (non-Linux, old kernels) a plain kill() follows
    the same check.
    """
    opener = getattr(os, "pidfd_open", None)
    sender = getattr(signal, "pidfd_send_signal", None)
    if opener is None or sender is None:
        if not still_ours(pid):
            raise ProcessLookupError(pid)
        os.kill(pid, signal.SIGTERM)
        return
    fd = opener(pid)
    try:
        if not still_ours(pid):
            raise ProcessLookupError(pid)
        sender(fd, signal.SIGTERM)
    finally:
        os.close(fd)

@dataclass(slots=True)
class System:
    """Everything that touches the system, replaceable in tests."""

    which: Callable[[str], str | None] = shutil.which
    desktop_app: Callable[[str], Launcher | None] = gio_desktop_app
    command_app: Callable[[Sequence[str]], Launcher] = gio_command_app
    run: Callable[[Sequence[str], float], CommandResult] = run_command
    open_uri: Callable[[str], None] = gio_open_uri
    hostname: Callable[[], str] = socket.gethostname
    runtime_dir: Callable[[], Path] = runtime_private_directory
    process_argv: Callable[[int], list[str] | None] = process_argv
    # SIGTERM to a pid while it is still the process we started (pidfd).
    terminate: Callable[[int, Callable[[int], bool]], None] = pidfd_terminate
    is_mount: Callable[[Path], bool] = os.path.ismount
    replaced_tools: Callable[[], frozenset[str]] = replaced_by_plugins


def default_system() -> System:
    """The real system. Clients call this (not ``System()``) so the test
    suite can swap in an inert one for every client at once."""
    return System()


@dataclass(frozen=True, slots=True)
class ToolState:
    """One entry in the card's Tools section and in ``blueferry tools``."""

    key: str
    installed: bool
    enabled: bool
    title: str
    subtitle: str
    active: bool = False


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str
    needs_pairing: bool = False


@dataclass(frozen=True, slots=True)
class PhotoProbe:
    """What :func:`probe_photos` found; ``device`` is False until probed."""

    installed: bool = False
    device: bool = False
    mounted: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    tools: tuple[ToolState, ...] = field(default_factory=tuple)

    def get(self, key: str) -> ToolState | None:
        return next((tool for tool in self.tools if tool.key == key), None)


# ---- screen mirroring (UxPlay) ---------------------------------------------

MIRROR_HINT = _(
    "On the iPhone, open Control Center, tap Screen Mirroring and choose this computer."
)


def _mirror_launcher(system: System) -> Launcher | None:
    launcher = system.desktop_app(UXPLAY_DESKTOP_ID)
    if launcher is not None:
        return launcher
    if system.which(UXPLAY):
        # The AirPlay name; never an option for uxplay ("-n -x").
        name = system.hostname().split(".")[0].lstrip("-").strip() or "BlueFerry"
        return system.command_app([UXPLAY, "-n", name, "-nh"])
    return None


def _mirror_installed(system: System) -> bool:
    return system.desktop_app(UXPLAY_DESKTOP_ID) is not None or bool(system.which(UXPLAY))


def _pid_file(system: System) -> Path:
    return system.runtime_dir() / MIRROR_PID_FILE


def _runs_uxplay(system: System, pid: int) -> bool:
    argv = system.process_argv(pid) if pid > 1 and pid != os.getpid() else None
    return argv is not None and any(os.path.basename(part) == UXPLAY for part in argv[:3])


def mirroring_pid(system: System) -> int | None:
    """The UxPlay process this user's BlueFerry clients started, if it runs.

    The id comes from our own record, not from a process search, and only
    counts while that process still runs ``uxplay``; a stale record is
    removed.
    """
    path = _pid_file(system)
    try:
        pid = int(read_private_text(path, maximum_bytes=32).strip())
    except (OSError, ValueError):
        return None
    if _runs_uxplay(system, pid):
        return pid
    path.unlink(missing_ok=True)
    return None


def mirror_state(system: System) -> ToolState:
    installed = _mirror_installed(system)
    running = installed and mirroring_pid(system) is not None
    if not installed:
        return ToolState(
            MIRROR, False, False, _("Mirror iPhone screen"),
            _("Install UxPlay to show the iPhone's screen on this computer."),
        )
    if running:
        return ToolState(
            MIRROR, True, True, _("Stop mirroring"),
            _("UxPlay is waiting for the iPhone or showing its screen."), active=True,
        )
    return ToolState(MIRROR, True, True, _("Mirror iPhone screen"), MIRROR_HINT)


def toggle_mirroring(system: System) -> ActionResult:
    """Start UxPlay, or stop the one started earlier. Does not block."""
    pid = mirroring_pid(system)
    if pid is not None:
        try:
            system.terminate(pid, lambda candidate: _runs_uxplay(system, candidate))
        except ProcessLookupError:
            pass
        except OSError:
            return ActionResult(False, _("Could not stop screen mirroring."))
        _pid_file(system).unlink(missing_ok=True)
        return ActionResult(True, _("Screen mirroring stopped."))
    launcher = _mirror_launcher(system)
    if launcher is None:
        return ActionResult(False, _("UxPlay is not installed."))
    try:
        started = launcher.launch()
    except Exception:  # GLib.Error and friends: report, never crash a client
        return ActionResult(False, _("Could not start UxPlay."))
    if started is not None:
        atomic_write_private_text(_pid_file(system), str(started), maximum_bytes=32)
    return ActionResult(True, _("Screen mirroring is ready. {hint}").format(hint=MIRROR_HINT))


# ---- LocalSend ---------------------------------------------------------------

def _send_launcher(system: System) -> Launcher | None:
    launcher = system.desktop_app(LOCALSEND_DESKTOP_ID)
    if launcher is not None:
        return launcher
    if system.which(LOCALSEND):
        return system.command_app([LOCALSEND])
    return None


def send_state(system: System) -> ToolState:
    title = _("Send a file (LocalSend)")
    if system.desktop_app(LOCALSEND_DESKTOP_ID) is None and not system.which(LOCALSEND):
        return ToolState(SEND, False, False, title,
                         _("Install LocalSend (Flathub) to exchange files over Wi-Fi."))
    return ToolState(SEND, True, True, title,
                     _("Open LocalSend on both devices in the same Wi-Fi."))


def start_localsend(system: System) -> ActionResult:
    launcher = _send_launcher(system)
    if launcher is None:
        return ActionResult(False, _("LocalSend is not installed."))
    try:
        launcher.launch()
    except Exception:  # GLib.Error: report, never crash a client
        return ActionResult(False, _("Could not start LocalSend."))
    return ActionResult(True, _("LocalSend started."))


# ---- iPhone photos over USB (ifuse) -----------------------------------------

PAIRING_HINT = _(
    "Unlock the iPhone and confirm “Trust This Computer”, then try again."
)


def photos_dir(system: System) -> Path:
    return system.runtime_dir() / PHOTOS_DIR


def _photo_tools(system: System) -> bool:
    return bool(system.which(IFUSE)) and bool(system.which(IDEVICE_ID))


def _fusermount(system: System) -> str | None:
    return next((name for name in FUSERMOUNT if system.which(name)), None)


def probe_photos(system: System) -> PhotoProbe:
    """*Blocking.* Whether ifuse is usable and an iPhone is on USB."""
    if not _photo_tools(system):
        return PhotoProbe()
    mounted = system.is_mount(photos_dir(system))
    listed = system.run([IDEVICE_ID, "-l"], PROBE_TIMEOUT_SEC)
    device = listed.ok and any(line.strip() for line in listed.output.splitlines())
    return PhotoProbe(installed=True, device=device, mounted=mounted)


def photos_states(probe: PhotoProbe) -> tuple[ToolState, ToolState]:
    title = _("iPhone photos (USB)")
    if not probe.installed:
        photos = ToolState(PHOTOS, False, False, title,
                           _("Install ifuse and libimobiledevice to browse the camera roll."))
    elif probe.mounted:
        photos = ToolState(PHOTOS, True, True, title,
                           _("The camera roll is open. Eject it before unplugging."), active=True)
    elif not probe.device:
        photos = ToolState(PHOTOS, True, False, title,
                           _("Connect the unlocked iPhone with a USB cable."))
    else:
        photos = ToolState(PHOTOS, True, True, title,
                           _("Open the camera roll in the file manager."))
    eject = ToolState(
        EJECT, probe.installed, probe.mounted, _("Eject iPhone photos"),
        _("Unmount the camera roll.") if probe.mounted else _("Nothing is mounted."),
    )
    return photos, eject


def _pairing_problem(output: str) -> bool:
    text = output.casefold()
    return any(word in text for word in ("pair", "trust", "passcode", "lockdown"))


def _open_folder(system: System, root: Path) -> ActionResult:
    dcim = root / "DCIM"
    target = dcim if dcim.is_dir() else root
    try:
        system.open_uri(target.as_uri())
    except Exception:  # GLib.Error: no file manager registered
        return ActionResult(False, _("The photos are mounted at {path}, but no file manager "
                                     "could be opened.").format(path=target))
    return ActionResult(True, _("iPhone photos opened. Choose Eject when you are done."))


def open_photos(system: System) -> ActionResult:
    """*Blocking.* Mount the camera roll (once) and open its DCIM folder."""
    if not _photo_tools(system):
        return ActionResult(False, _("ifuse and libimobiledevice are not installed."))
    root = photos_dir(system)
    if system.is_mount(root):
        return _open_folder(system, root)
    listed = system.run([IDEVICE_ID, "-l"], PROBE_TIMEOUT_SEC)
    if not (listed.ok and listed.output.strip()):
        return ActionResult(False, _("No iPhone is connected over USB."))
    if system.which(IDEVICEPAIR):
        validated = system.run([IDEVICEPAIR, "validate"], VALIDATE_TIMEOUT_SEC)
        if not validated.ok:
            return ActionResult(False, _("This computer is not trusted by the iPhone yet. ")
                                + PAIRING_HINT, needs_pairing=True)
    try:
        ensure_private_directory(root)
    except OSError:
        return ActionResult(False, _("Could not prepare the folder for the iPhone photos."))
    mounted = system.run([IFUSE, str(root)], MOUNT_TIMEOUT_SEC)
    if mounted.timed_out:
        return ActionResult(False, _("The iPhone did not answer in time. Unlock it and try again."))
    if not mounted.ok or not system.is_mount(root):
        if _pairing_problem(mounted.output):
            return ActionResult(False, PAIRING_HINT, needs_pairing=True)
        return ActionResult(False, _("Could not open the iPhone photos. Unlock the iPhone, "
                                     "reconnect the cable and try again."))
    return _open_folder(system, root)


def eject_photos(system: System) -> ActionResult:
    """*Blocking.* Unmount the camera roll."""
    root = photos_dir(system)
    if not system.is_mount(root):
        return ActionResult(True, _("No iPhone photos are mounted."))
    tool = _fusermount(system)
    if tool is None:
        return ActionResult(False, _("fusermount3 is not installed."))
    result = system.run([tool, "-u", str(root)], UNMOUNT_TIMEOUT_SEC)
    if result.ok:
        return ActionResult(True, _("iPhone photos ejected. You can unplug the iPhone."))
    if "busy" in result.output.casefold():
        return ActionResult(False, _("The photos are still in use. Close windows and programs "
                                     "showing them, then eject again."))
    return ActionResult(False, _("Could not eject the iPhone photos."))


def pair_iphone(system: System) -> ActionResult:
    """*Blocking.* Ask the iPhone to trust this computer (``idevicepair pair``)."""
    if not system.which(IDEVICEPAIR):
        return ActionResult(False, _("idevicepair is not installed."))
    result = system.run([IDEVICEPAIR, "pair"], PAIR_TIMEOUT_SEC)
    if result.ok:
        return ActionResult(True, _("The iPhone trusts this computer now. Open the photos again."))
    if result.timed_out:
        return ActionResult(False, _("The iPhone did not answer in time."), needs_pairing=True)
    return ActionResult(False, PAIRING_HINT, needs_pairing=True)


# ---- shared entry points ------------------------------------------------------

# Manifest names (ReplacesTools) of the tools a plugin can stand in for.
_TOOL_NAMES = {SEND: "localsend", MIRROR: "uxplay"}


def replaced(system: System) -> frozenset[str]:
    """Keys of the tools an enabled plugin replaces. *Blocking* (manifests)."""
    names = system.replaced_tools()
    return frozenset(key for key, name in _TOOL_NAMES.items() if name in names)


def snapshot(system: System, probe: PhotoProbe | None = None) -> Snapshot:
    """*Blocking*: the state of every entry no plugin replaces."""
    photos, eject = photos_states(probe if probe is not None else probe_photos(system))
    hidden = replaced(system)
    return Snapshot(tuple(
        tool for tool in (mirror_state(system), send_state(system), photos, eject)
        if tool.key not in hidden
    ))


REPLACED_SEND_HINT = _(
    "LocalSend runs inside BlueFerry now: choose “Send files…” on a device "
    "under From Plugins, or run “blueferry send FILE”.")


def perform(
    system: System, action: str, *, hidden: frozenset[str] | None = None,
) -> ActionResult:
    """Run one action. *Blocking* for the actions in BLOCKING_ACTIONS, and
    for mirror and send unless ``hidden`` (the keys a plugin replaces, e.g.
    from the last snapshot) is given; otherwise the manifests are read."""
    handlers: dict[str, Callable[[System], ActionResult]] = {
        MIRROR: toggle_mirroring,
        SEND: start_localsend,
        PHOTOS: open_photos,
        EJECT: eject_photos,
        PAIR: pair_iphone,
    }
    handler = handlers.get(action)
    if handler is None:
        return ActionResult(False, _("Unknown tool {name!r}.").format(name=action))
    if action in _TOOL_NAMES and action in (replaced(system) if hidden is None else hidden):
        if action == SEND:
            return ActionResult(False, REPLACED_SEND_HINT)
        return ActionResult(False, _("A BlueFerry plugin replaces this tool."))
    return handler(system)
