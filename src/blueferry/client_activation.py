"""Choose and activate one desktop client, independently of the daemon's lifetime.

Clients own a session-bus name while running and each writes its own recency
file. This avoids concurrent GUI writes to the daemon's settings document.
The command-line entry point also serves shells that retain an executable
notification action instead of delivering ActionInvoked to the daemon.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import dbus
import dbus.mainloop

from blueferry import config
from blueferry.private_files import atomic_write_private_text, read_private_text
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE, OBJECT_PATH

log = logging.getLogger(__name__)
ACTIVATION_INTERFACE = "io.weirdware.BlueFerry.Client"
ACTIVATION_PATH = "/io/weirdware/BlueFerry/Client"
# Owned by the standalone ``blueferry-tray`` item while it runs.
TRAY_BUS_NAME = f"{ACTIVATION_INTERFACE}.Tray"
_recency_lock = threading.Lock()


@dataclass(frozen=True)
class DesktopClient:
    key: str
    desktop_id: str

    @property
    def bus_name(self) -> str:
        return f"{ACTIVATION_INTERFACE}.{self.desktop_id.rsplit('.', 1)[1]}"

    @property
    def program_name(self) -> str:
        """The command name; ``client_program`` finds where it is installed."""
        return f"blueferry-{self.key}"


CLIENTS = (
    DesktopClient("gtk", "io.weirdware.BlueFerry.Gtk"),
    DesktopClient("qt", "io.weirdware.BlueFerry.Qt"),
    DesktopClient("quickshell", "io.weirdware.BlueFerry.Quickshell"),
)
GTK_CLIENT = next(client for client in CLIENTS if client.key == "gtk")


def _recency_path(client: DesktopClient) -> Path:
    return config.CONFIG_DIR / "clients" / client.key


def record_client_use(key: str) -> None:
    client = next(client for client in CLIENTS if client.key == key)
    try:
        # GTK/Qt record on the GUI thread; QS also receives focus on its stdin
        # reader. Keep a slower write from replacing a newer timestamp.
        with _recency_lock:
            atomic_write_private_text(_recency_path(client), str(time.time_ns()), maximum_bytes=64)
    except OSError:
        log.warning("could not remember the active desktop client", exc_info=True)


def _last_used(client: DesktopClient) -> int:
    try:
        return max(0, int(read_private_text(_recency_path(client), maximum_bytes=64)))
    except (OSError, ValueError):
        return 0


def _preferred_key(environment: Mapping[str, str]) -> str:
    desktop = environment.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if "gnome" in desktop or "unity" in desktop:
        return "gtk"
    if "kde" in desktop:
        return "qt"
    if "hyprland" in desktop or environment.get("OMARCHY_PATH"):
        return "quickshell"
    return "gtk"


def is_live(client: DesktopClient, running: Sequence[str]) -> bool:
    """Whether ``client`` runs; a pre-upgrade GTK only owns its application ID."""
    return client.bus_name in running or (
        client == GTK_CLIENT and client.desktop_id in running
    )


_SYSTEM_BIN = Path("/usr/bin")
# Clients that can also run from this Python installation, and the module
# dependency lives in for this interpreter.
_CLIENT_MODULES = {"gtk": ("blueferry.ui.app", "gi"), "qt": ("blueferry.qt.app", "PySide6")}


def _dependency_available(client: DesktopClient) -> bool:
    """Whether the GTK/Qt toolkit is importable here; nothing is imported.

    A virtual environment always gets both launchers, while the toolkits may
    come from the distribution, so a launcher alone does not mean it runs.
    """
    entry = _CLIENT_MODULES.get(client.key)
    if entry is None:
        return True
    try:
        return importlib.util.find_spec(entry[1]) is not None
    except (ImportError, ValueError):
        return False


def _module_program(client: DesktopClient) -> list[str] | None:
    """``python -m`` for a client shipped in this installation."""
    entry = _CLIENT_MODULES.get(client.key)
    if entry is None:
        return None
    module = entry[0]
    # Look for the file instead of importing the client's package.
    path = Path(__file__).resolve().parent.joinpath(*module.split(".")[1:]).with_suffix(".py")
    return [sys.executable, "-m", module] if path.is_file() else None


def is_module_program(program: Sequence[str]) -> bool:
    """Whether ``program`` is the ``python -m`` fallback of ``client_program``."""
    return len(program) == 3 and program[0] == sys.executable and program[1] == "-m"


def client_program(client: DesktopClient) -> list[str] | None:
    """The absolute command that starts ``client``, or None if it is missing.

    PATH covers package, ``pipx`` and ``~/.local/bin`` installs. The
    directory of this interpreter covers a virtual environment that is not
    on the PATH of whoever started the daemon (a D-Bus activated daemon on
    OpenRC, or the systemd user manager's PATH), and the module fallback a
    client installed in this environment without its launcher. The result is
    absolute, so the systemd transient unit runs the same file. Launchers
    beside this interpreter and module fallbacks need its toolkit. External
    launchers use their own interpreter, which may have different packages.
    """
    name = client.program_name
    interpreter_dir = Path(os.path.abspath(sys.executable)).parent
    directories = [Path(os.path.abspath(path)) for path in os.get_exec_path()]
    directories.extend((interpreter_dir, Path(os.path.abspath(_SYSTEM_BIN))))
    for directory in dict.fromkeys(directories):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            if directory == interpreter_dir and not _dependency_available(client):
                continue
            return [str(candidate)]
    return _module_program(client) if _dependency_available(client) else None


def _installed(client: DesktopClient) -> bool:
    return client_program(client) is not None


def ranked_clients(
    running: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    installed: Callable[[DesktopClient], bool] = _installed,
) -> list[DesktopClient]:
    """Every usable client, best first: running ones, then installed ones.

    Within each group the most recently used client wins, then the one that
    suits the current desktop.
    """
    preferred = _preferred_key(os.environ if environment is None else environment)

    def ordered(clients: list[DesktopClient]) -> list[DesktopClient]:
        # sorted() is stable, so ties keep the CLIENTS order, as max() did.
        return sorted(
            clients, key=lambda client: (_last_used(client), client.key == preferred),
            reverse=True,
        )

    live = [client for client in CLIENTS if is_live(client, running)]
    idle = [client for client in CLIENTS if client not in live and installed(client)]
    return ordered(live) + ordered(idle)


def select_client(
    running: Sequence[str], *, environment: Mapping[str, str] | None = None,
) -> DesktopClient | None:
    return next(iter(ranked_clients(running, environment=environment)), None)


def activation_argv(handle: str) -> list[str]:
    return [sys.executable, "-m", "blueferry.client_activation", f"--message={handle}"]


def activation_environment(token: str) -> dict[str, str]:
    """This process's environment with only the given single-use token."""
    environment = dict(os.environ)
    for name in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID"):
        environment.pop(name, None)
    if token:
        environment["XDG_ACTIVATION_TOKEN"] = token
        environment["DESKTOP_STARTUP_ID"] = token
    return environment


_activation_environment = activation_environment


def client_environment(program: Sequence[str], token: str) -> dict[str, str]:
    """Keep a module fallback importable when the user manager changes cwd."""
    environment = activation_environment(token)
    if is_module_program(program):
        paths = [str(Path(__file__).resolve().parent.parent)]
        if "PYTHONPATH" in environment:
            paths.extend(
                os.path.abspath(path) for path in environment["PYTHONPATH"].split(os.pathsep)
            )
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return environment


# Session variables a GUI started by the systemd user manager needs; the
# manager's own environment may predate the graphical session.
TRANSIENT_SESSION_KEYS = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
)


def start_transient_service(
    bus,
    name_prefix: str,
    argv: Sequence[str],
    variables: Sequence[str],
    extra_properties: Sequence[tuple[str, object]] = (),
) -> None:
    """Ask the systemd user manager to run ``argv`` outside the backend.

    ExecStartEx with ``no-env-expand`` disables systemd's $VARIABLE
    substitution, so every argument arrives unchanged, just as with execve.
    """
    manager = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
    manager.StartTransientUnit(
        f"{name_prefix}-{uuid.uuid4().hex}.service", "fail",
        dbus.Array([
            ("Type", "exec"), ("CollectMode", "inactive-or-failed"),
            *extra_properties,
            ("ExecStartEx", dbus.Array([
                (argv[0], list(argv), ["no-env-expand"]),
            ], signature="(sasas)")),
            ("Environment", dbus.Array(list(variables), signature="s")),
        ], signature="(sv)"),
        dbus.Array([], signature="(sa(sv))"),
        dbus_interface="org.freedesktop.systemd1.Manager", timeout=3,
    )


# What a ``python -m`` client additionally needs to find its modules.
MODULE_SESSION_KEYS = ("PYTHONPATH", "VIRTUAL_ENV")


def transient_unit_environment(
    environment: Mapping[str, str], token: str, *, extra_keys: Sequence[str] = (),
) -> list[str]:
    """``Environment=`` assignments for a GUI launched outside the backend."""
    variables = [
        f"{name}={environment[name]}"
        for name in (*TRANSIENT_SESSION_KEYS, *extra_keys) if name in environment
    ]
    variables.extend(
        f"{name}={token}" for name in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")
    )
    return variables


def request_message_activation(handle: str, token: str) -> None:
    """Keep client startup and unresponsive GUI calls off the daemon's GLib loop."""
    if not handle or len(handle) > 1024 or len(token) > 4096:
        return
    # Tokens are single-use; never inherit an earlier startup's token.
    try:
        # Fixed module; the opaque message handle cannot become shell code.
        subprocess.Popen(  # nosec B603
            activation_argv(handle), env=_activation_environment(token), stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        log.exception("could not start desktop client activation")


def _open_legacy_gtk(bus, handle: str, token: str) -> bool:
    """Keep a pre-upgrade GTK window (and its drafts) alive while activating it."""
    gtk = bus.get_object(
        GTK_CLIENT.desktop_id, "/" + GTK_CLIENT.desktop_id.replace(".", "/"), introspect=False,
    )
    platform_data = dbus.Dictionary({}, signature="sv")
    if token:
        platform_data["activation-token"] = token
        platform_data["desktop-startup-id"] = token
    gtk.Activate(platform_data, dbus_interface="org.freedesktop.Application", timeout=3)
    if handle:
        # An up-to-date GTK process may have been between acquiring its
        # GApplication name and registering our endpoint. Activate returns
        # after startup, so retry the modern path before using the relay.
        if bus.name_has_owner(GTK_CLIENT.bus_name):
            bus.get_object(GTK_CLIENT.bus_name, ACTIVATION_PATH, introspect=False).OpenMessage(
                handle, "", dbus_interface=ACTIVATION_INTERFACE, timeout=3,
            )
            record_client_use("gtk")
            return True
        # Only the daemon can emit the sender-authenticated legacy signal.
        # GTK's Gio application and dbus-python listener use different bus
        # connections; the daemon targets connections belonging to this PID.
        daemon = bus.get_object(BUS_NAME, OBJECT_PATH, introspect=False)
        if not daemon.OpenLegacyGtkMessage(
            handle, gtk.bus_name, dbus_interface=MESSAGES_IFACE, timeout=3,
        ):
            return False
    record_client_use("gtk")
    return True


def forward_to_legacy_gtk(handle: str, token: str) -> bool | None:
    """Preflight GTK's command line: old GApplications cannot receive it.

    None means normal GApplication startup/forwarding should continue.
    """
    bus = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
    try:
        running = bus.list_names()
        if GTK_CLIENT.bus_name in running or GTK_CLIENT.desktop_id not in running:
            return None
        try:
            forwarded = _open_legacy_gtk(bus, handle, token)
        except dbus.DBusException:
            if bus.name_has_owner(GTK_CLIENT.desktop_id):
                raise
            return None
        if not forwarded and not bus.name_has_owner(GTK_CLIENT.desktop_id):
            return None  # the old window closed; continue normal GTK startup
        return forwarded
    finally:
        bus.close()


def open_message(handle: str, token: str) -> bool:
    """Run in a short-lived helper; focus a live client or launch the selected one."""
    bus = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
    try:
        running = bus.list_names()
        client = select_client(running)
        if client is None:
            log.warning("no BlueFerry graphical client is installed")
            return False
        live_name = (
            client.bus_name if client.bus_name in running
            else client.desktop_id if client == GTK_CLIENT and client.desktop_id in running
            else None
        )
        if live_name is not None:
            try:
                if live_name == GTK_CLIENT.desktop_id:
                    forwarded = _open_legacy_gtk(bus, handle, token)
                    if forwarded or bus.name_has_owner(live_name):
                        return forwarded
                else:
                    bus.get_object(client.bus_name, ACTIVATION_PATH, introspect=False).OpenMessage(
                        handle, token, dbus_interface=ACTIVATION_INTERFACE, timeout=3,
                    )
                    return True
            except dbus.DBusException:
                # A GUI can exit after selection. Only relaunch when its name
                # vanished; a timeout must not open a second, competing client.
                if bus.name_has_owner(live_name):
                    log.warning("the running %s client did not accept activation", client.key)
                    return False
        program = client_program(client)
        if program is None:
            log.warning("the %s client is no longer installed", client.key)
            return False
        argv = [*program, f"--message={handle}"]
        environment = client_environment(program, token)
        if "org.freedesktop.systemd1" in running:
            # A child of blueferry.service inherits PrivateDevices/PrivateTmp
            # and dies when the backend restarts. Let the user manager create
            # the GUI outside the backend's sandbox and cgroup instead.
            start_transient_service(
                bus, f"app-blueferry-{client.key}", argv,
                transient_unit_environment(
                    environment, token,
                    # An uninstalled checkout only imports with its own path.
                    extra_keys=MODULE_SESSION_KEYS if is_module_program(program) else (),
                ),
            )
        else:
            # A manually run daemon on a desktop without a systemd user manager.
            subprocess.Popen(  # nosec B603
                argv, env=environment, stdin=subprocess.DEVNULL, start_new_session=True,
            )
        return True
    finally:
        bus.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a BlueFerry message in a desktop client")
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    token = os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID", "")
    if not args.message or len(args.message) > 1024 or len(token) > 4096:
        parser.error("invalid message handle or activation token")
    try:
        return 0 if open_message(args.message, token) else 1
    except (OSError, dbus.DBusException):
        log.exception("could not activate a BlueFerry desktop client")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
