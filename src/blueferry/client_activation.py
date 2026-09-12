"""Choose and activate one desktop client, independently of the daemon's lifetime.

Clients own a session-bus name while running and each writes its own recency
file. This avoids concurrent GUI writes to the daemon's settings document.
The command-line entry point also serves shells that retain an executable
notification action instead of delivering ActionInvoked to the daemon.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
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
_recency_lock = threading.Lock()


@dataclass(frozen=True)
class DesktopClient:
    key: str
    desktop_id: str

    @property
    def bus_name(self) -> str:
        return f"{ACTIVATION_INTERFACE}.{self.desktop_id.rsplit('.', 1)[1]}"

    @property
    def executable(self) -> str:
        return f"/usr/bin/blueferry-{self.key}"


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


def select_client(
    running: Sequence[str], *, environment: Mapping[str, str] | None = None,
) -> DesktopClient | None:
    environment = os.environ if environment is None else environment
    desktop = environment.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if "gnome" in desktop or "unity" in desktop:
        preferred = "gtk"
    elif "kde" in desktop:
        preferred = "qt"
    elif "hyprland" in desktop or environment.get("OMARCHY_PATH"):
        preferred = "quickshell"
    else:
        preferred = "gtk"
    live = [client for client in CLIENTS if (
        client.bus_name in running
        or (client == GTK_CLIENT and client.desktop_id in running)
    )]
    candidates = live or [client for client in CLIENTS if os.access(client.executable, os.X_OK)]
    return max(
        candidates, key=lambda client: (_last_used(client), client.key == preferred), default=None,
    )


def activation_argv(handle: str) -> list[str]:
    return [sys.executable, "-m", "blueferry.client_activation", f"--message={handle}"]


def _activation_environment(token: str) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID"):
        environment.pop(name, None)
    if token:
        environment["XDG_ACTIVATION_TOKEN"] = token
        environment["DESKTOP_STARTUP_ID"] = token
    return environment


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
        argv = [client.executable, f"--message={handle}"]
        environment = _activation_environment(token)
        if "org.freedesktop.systemd1" in running:
            # A child of blueferry.service inherits PrivateDevices/PrivateTmp
            # and dies when the backend restarts. Let the user manager create
            # the GUI outside the backend's sandbox and cgroup instead.
            manager = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
            session_keys = (
                "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
            )
            variables = [
                f"{name}={environment[name]}" for name in session_keys if name in environment
            ]
            variables.extend(
                f"{name}={token}" for name in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")
            )
            manager.StartTransientUnit(
                f"app-blueferry-{client.key}-{uuid.uuid4().hex}.service", "fail",
                dbus.Array([
                    ("Type", "exec"), ("CollectMode", "inactive-or-failed"),
                    # ExecStartEx disables systemd's $VARIABLE substitution;
                    # the handle must arrive unchanged, just as with execve.
                    ("ExecStartEx", dbus.Array([
                        (client.executable, argv, ["no-env-expand"]),
                    ], signature="(sasas)")),
                    ("Environment", dbus.Array(variables, signature="s")),
                ], signature="(sv)"),
                dbus.Array([], signature="(sa(sv))"),
                dbus_interface="org.freedesktop.systemd1.Manager", timeout=3,
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
