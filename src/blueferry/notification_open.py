"""Open the user-configured app or URL for a clicked iPhone notification.

The daemon never launches desktop software on its own GLib loop or inside its
systemd sandbox (no IP networking, private /tmp and /dev, NoNewPrivileges).
Like ``client_activation``, a click starts one short-lived helper process:

1. The daemon spawns ``python -m blueferry.notification_open --url=...`` or
   ``--desktop-id=...`` with the notification server's activation token in
   its environment.
2. When a systemd user manager is on the session bus, the helper asks it to
   start a transient service that reruns the helper with ``--direct``
   outside the backend's cgroup and sandbox. Without systemd the helper was
   not sandboxed to begin with and launches directly.
3. ``--direct`` launches through Gio: ``AppInfo.launch_default_for_uri`` for
   http(s) URLs, ``DesktopAppInfo`` for desktop-entry IDs. Neither uses a
   shell, and desktop entries are launched without files or URIs.

Only the validated, user-configured target ever crosses these boundaries;
notification content is never passed along. Each boundary revalidates.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess  # nosec B404
import sys
from collections.abc import Callable
from typing import Any

import dbus
import dbus.mainloop

from blueferry.client_activation import (
    activation_environment,
    start_transient_service,
    transient_unit_environment,
)
from blueferry.notification_open_map import (
    DESKTOP_TARGET,
    URL_TARGET,
    OpenTarget,
    parse_target,
)

log = logging.getLogger(__name__)

MODULE = "blueferry.notification_open"
MAX_ACTIVATION_TOKEN_CHARS = 4096
_SYSTEMD_NAME = "org.freedesktop.systemd1"
_UNKNOWN_PROPERTY_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.PropertyReadOnly",
    "org.freedesktop.DBus.Error.InvalidArgs",
})


def _checked(target: OpenTarget) -> OpenTarget | None:
    try:
        checked = parse_target(target.value)
    except ValueError:
        return None
    return checked if checked == target else None


def helper_argv(target: OpenTarget, *, direct: bool = False) -> list[str]:
    """Fixed module argv; the target is one ``--flag=value`` token, never parsed by a shell."""
    flag = "--url" if target.kind == URL_TARGET else "--desktop-id"
    argv = [sys.executable, "-m", MODULE]
    if direct:
        argv.append("--direct")
    argv.append(f"{flag}={target.value}")
    return argv


def request_open_target(
    target: OpenTarget,
    token: str,
    *,
    spawn: Callable[..., object] = subprocess.Popen,
) -> bool:
    """Daemon side: start the helper without blocking the GLib main loop."""
    checked = _checked(target)
    if checked is None or len(token) > MAX_ACTIVATION_TOKEN_CHARS:
        log.warning("refusing to open an invalid notification click target")
        return False
    try:
        # Fixed module; the validated target is one argv token, never shell.
        spawn(
            helper_argv(checked),
            env=activation_environment(token),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        log.exception("could not start the notification click helper")
        return False
    return True


def _start_transient_unit(bus: Any, target: OpenTarget, token: str) -> None:
    argv = helper_argv(target, direct=True)
    # PATH and the XDG search/desktop variables come from the user manager's
    # own environment, exactly as for BlueFerry's clients; the daemon's
    # sandboxed values must not override the session's defaults.
    variables = transient_unit_environment(os.environ, token)
    try:
        # Unlike a BlueFerry client, the helper exits right after Gio has
        # spawned the application, so the unit must live until its cgroup is
        # empty; otherwise systemd would kill the app it just started.
        start_transient_service(
            bus, "app-blueferry-open", argv, variables, [("ExitType", "cgroup")],
        )
    except dbus.DBusException as error:
        # systemd < 250 rejects ExitType as an unknown property. Only then
        # retry (a timeout must not launch twice), leaving the application
        # running when the helper, the unit's main process, exits.
        if error.get_dbus_name() not in _UNKNOWN_PROPERTY_ERRORS:
            raise
        start_transient_service(
            bus, "app-blueferry-open", argv, variables, [("KillMode", "process")],
        )


def _private_bus() -> Any:
    return dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)


_CONTEXT_CLASSES: dict[int, type] = {}


def _token_context_class(gio: Any) -> type:
    """An AppLaunchContext that hands Gio the notification's activation token.

    Environment variables only reach applications Gio spawns. For
    DBusActivatable entries Gio instead asks the context for a startup ID
    and forwards it as ``activation-token`` platform data, so the context
    must return the token itself. One subclass is registered per Gio module.
    """
    cached = _CONTEXT_CLASSES.get(id(gio))
    if cached is not None:
        return cached

    class TokenLaunchContext(gio.AppLaunchContext):
        def __init__(self, token: str) -> None:
            super().__init__()
            self._activation_token = token

        def do_get_startup_notify_id(self, _info, _files):
            return self._activation_token or None

    _CONTEXT_CLASSES[id(gio)] = TokenLaunchContext
    return TokenLaunchContext


def launch_target(target: OpenTarget, token: str, *, gio: Any = None) -> bool:
    """Outside any sandbox: hand the target to Gio with the activation token."""
    checked = _checked(target)
    if checked is None:
        return False
    if gio is None:
        from gi.repository import Gio

        gio = Gio
    context = _token_context_class(gio)(token)
    if token:
        context.setenv("XDG_ACTIVATION_TOKEN", token)
        context.setenv("DESKTOP_STARTUP_ID", token)
    if checked.kind == URL_TARGET:
        return bool(gio.AppInfo.launch_default_for_uri(checked.value, context))
    info = gio.DesktopAppInfo.new(checked.value)
    if info is None:
        log.warning("the configured desktop entry is not installed")
        return False
    return bool(info.launch([], context))


def open_target(
    target: OpenTarget,
    token: str,
    *,
    bus_factory: Callable[[], Any] = _private_bus,
    launcher: Callable[[OpenTarget, str], bool] = launch_target,
) -> bool:
    """Helper side: leave the backend's sandbox via systemd, or launch directly."""
    checked = _checked(target)
    if checked is None:
        return False
    try:
        bus = bus_factory()
    except (dbus.DBusException, OSError):
        # No reachable session bus means no systemd user manager to ask;
        # launching directly is then the only (and unsandboxed) option.
        log.warning("session bus unavailable; opening the click target directly")
        return launcher(checked, token)
    try:
        if _SYSTEMD_NAME in bus.list_names():
            _start_transient_unit(bus, checked, token)
            return True
    finally:
        bus.close()
    return launcher(checked, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open the configured target for a clicked iPhone notification",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url")
    group.add_argument("--desktop-id")
    parser.add_argument("--direct", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    token = os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID", "")
    if len(token) > MAX_ACTIVATION_TOKEN_CHARS:
        parser.error("invalid activation token")
    expected = URL_TARGET if args.url is not None else DESKTOP_TARGET
    try:
        target = parse_target(args.url if args.url is not None else args.desktop_id)
    except ValueError as error:
        parser.error(str(error))
    if target.kind != expected:
        parser.error("target does not match its option")
    try:
        opened = launch_target(target, token) if args.direct else open_target(target, token)
    except dbus.DBusException as error:
        log.error("could not open the notification click target: %s", error.get_dbus_name())
        return 1
    except Exception as error:
        # Gio reports launch failures as GLib.Error. Its message can repeat
        # the configured URL, which may be private, so log only the type.
        log.error("could not open the notification click target: %s", type(error).__name__)
        return 1
    return 0 if opened else 1


if __name__ == "__main__":
    raise SystemExit(main())
