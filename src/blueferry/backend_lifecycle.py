"""Lifecycle helpers shared by the graphical clients.

The daemon is D-Bus activatable, so probing GetStatus starts it when needed.
Arch package upgrades cannot safely address every logged-in user's service
manager; the installed release marker lets a client recognize a process that
predates the files currently on disk and restart that one user service.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

import dbus
import dbus.exceptions

from blueferry.build_info import build_id, installed_build_sha
from blueferry.bus import get_session_bus
from blueferry.commands import run_command
from blueferry.errors import BlueFerryError, CommandError
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE, OBJECT_PATH

PACKAGE_RELEASE_PATH = Path("/usr/share/blueferry/package-release")
SERVICE = "blueferry.service"

log = logging.getLogger(__name__)


class BackendLifecycleError(BlueFerryError):
    pass


def installed_release() -> str | None:
    """Return the Arch package release, or None in a source checkout."""
    try:
        value = PACKAGE_RELEASE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _status(timeout: int = 20) -> dict:
    bus = get_session_bus()
    proxy = bus.get_object(BUS_NAME, OBJECT_PATH)
    iface = dbus.Interface(proxy, MESSAGES_IFACE)
    value = json.loads(str(iface.GetStatus(timeout=timeout)))
    if not isinstance(value, dict):
        raise BackendLifecycleError("backend returned an invalid status response")
    return value


def _systemctl(action: str) -> None:
    try:
        run_command(["/usr/bin/systemctl", "--user", action, SERVICE], timeout=45)
    except CommandError as error:
        raise BackendLifecycleError(str(error)) from error


def restart_backend() -> None:
    """Reload the packaged unit and restart this user's daemon."""
    try:
        run_command(["/usr/bin/systemctl", "--user", "daemon-reload"], timeout=20)
    except CommandError as error:
        raise BackendLifecycleError(str(error)) from error
    _systemctl("restart")


@contextmanager
def _restart_lock():
    """Serialize upgrade recovery among GTK, Qt, and shell clients."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        yield
        return
    path = Path(runtime_dir) / "blueferry-backend.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except OSError:
        yield
        return
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        flock(fd, LOCK_UN)
        os.close(fd)


def ensure_backend_current(
    status_reader: Callable[[], dict] | None = None,
) -> dict:
    """Start the backend on demand and replace a stale packaged process.

    The first GetStatus uses normal session D-Bus activation. A direct
    systemd start is retained as a fallback for sessions whose bus has not
    noticed a newly installed activation file yet. Toolkit clients may inject
    a reader backed by a connection owned by their worker thread.
    """
    read_status = status_reader or _status
    expected = installed_release()
    expected_sha = installed_build_sha()
    expected_build = build_id(expected, expected_sha) if expected is not None else None
    try:
        status = read_status()
    except (dbus.exceptions.DBusException, ValueError) as first_error:
        try:
            _systemctl("start")
            status = read_status()
        except (
            BackendLifecycleError,
            dbus.exceptions.DBusException,
            ValueError,
        ) as error:
            raise BackendLifecycleError(str(error) or str(first_error)) from error

    # No marker means a source checkout or a package transaction currently
    # replacing the file. Neither case justifies restarting a live service.
    if expected is None or (
        status.get("backend_release") == expected
        and (expected_sha is None or status.get("_build_id") == expected_build)
    ):
        return status

    # A missing key is expected from versions released before lifecycle
    # tracking. A different key means pacman replaced the files while this
    # user's old daemon was still alive.
    with _restart_lock():
        # Another client may have completed recovery while we waited.
        try:
            status = read_status()
        except (dbus.exceptions.DBusException, ValueError):
            status = {}
        if status.get("backend_release") != expected or (
            expected_sha is not None and status.get("_build_id") != expected_build
        ):
            restart_backend()
            try:
                status = read_status()
            except (dbus.exceptions.DBusException, ValueError) as error:
                raise BackendLifecycleError(str(error)) from error
    actual = status.get("backend_release")
    actual_build = status.get("_build_id")
    if actual != expected or (
        expected_sha is not None and actual_build != expected_build
    ):
        raise BackendLifecycleError(
            "backend build is "
            f"{actual_build or actual or 'unknown'}; installed build is "
            f"{expected_build or expected}"
        )
    return status
