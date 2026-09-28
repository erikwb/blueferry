"""Init-system boundary for BlueFerry's user service and session services.

systemd remains the reference: its commands are issued exactly as before this
module existed. OpenRC (0.62 and newer) can manage per-user services through
``rc-service --user``, but most OpenRC desktops start their session bus with
``dbus-run-session`` and never run BlueFerry as a user service. There the
session bus itself is the service manager: it starts the daemon through D-Bus
activation, and BlueFerry stops the process that owns its bus name.

Every command runs through the caller's ``run_command`` so each call site
keeps a single, testable command boundary.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from blueferry.errors import CommandError
from blueferry.protocol import BUS_NAME, MESSAGES_IFACE, OBJECT_PATH

SYSTEMD = "systemd"
OPENRC = "openrc"
NO_SERVICE_MANAGER = "none"
BUS_ACTIVATED = "dbus-activation"

SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUNTIME_DIR = Path("/run/systemd/system")
OPENRC_RUNTIME_DIR = Path("/run/openrc")
RC_SERVICE_CANDIDATES = (
    "/sbin/rc-service",
    "/usr/sbin/rc-service",
    "/usr/bin/rc-service",
    "/bin/rc-service",
)
BACKEND_SERVICE = "blueferry"
# Matches TimeoutStopSec in systemd/blueferry.service.
BACKEND_STOP_TIMEOUT_SECONDS = 180.0

# Matches the OpenRC script's retry="SIGTERM/180/SIGKILL/5".
BACKEND_KILL_WAIT_SECONDS = 5.0
_NAME_HAS_NO_OWNER = "org.freedesktop.DBus.Error.NameHasNoOwner"

RunCommand = Callable[..., Any]

log = logging.getLogger(__name__)


class ServiceManagerUnavailableError(CommandError):
    """No supported service manager can perform a lifecycle request."""

    def __init__(self, message: str) -> None:
        super().__init__((), message)


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def rc_service_path(candidates: Iterable[str] | None = None) -> str | None:
    """Return the first executable ``rc-service`` at a fixed system path."""
    paths = RC_SERVICE_CANDIDATES if candidates is None else candidates
    return next((path for path in paths if _executable(path)), None)


def detect_init_system(
    *,
    systemd_runtime: Path = SYSTEMD_RUNTIME_DIR,
    openrc_runtime: Path = OPENRC_RUNTIME_DIR,
    systemctl: str = SYSTEMCTL,
    rc_service_candidates: Iterable[str] | None = None,
) -> str:
    """Identify the system's service manager from filesystem markers.

    ``/run/systemd/system`` is systemd's documented "booted with systemd"
    marker; OpenRC creates ``/run/openrc`` at boot. When neither marker is
    present but ``systemctl`` exists, keep today's systemd behaviour so
    containers and chroots see exactly the commands they saw before.
    """
    # Hosts can have both init systems installed (Gentoo with systemd-utils,
    # or a switch in progress). Only the booted one leaves its runtime
    # marker, so markers decide before the bare-systemctl fallback.
    has_systemctl = _executable(systemctl)
    if systemd_runtime.is_dir() and has_systemctl:
        return SYSTEMD
    if openrc_runtime.is_dir() and rc_service_path(rc_service_candidates) is not None:
        return OPENRC
    if has_systemctl:
        return SYSTEMD
    return NO_SERVICE_MANAGER


def init_system() -> str:
    """Return the host's service manager. Tests pin this in conftest."""
    return detect_init_system()


def bluetooth_restart_command() -> str | None:
    """Return the command that restarts the system BlueZ daemon, if known."""
    kind = init_system()
    if kind == SYSTEMD:
        return "sudo systemctl restart bluetooth.service"
    if kind == OPENRC:
        return "sudo rc-service bluetooth restart"
    return None


def openrc_user_session() -> bool:
    """Return whether an OpenRC user session (pam_openrc) runs for this user."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and (Path(runtime) / "openrc").is_dir()


def openrc_session_bus() -> bool:
    """Return whether this process uses the bus the OpenRC script hardcodes."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    expected = f"unix:path={runtime}/bus"
    return bool(runtime) and (address == expected or address.startswith(expected + ","))


def openrc_backend_enabled() -> bool:
    """Return whether the user added BlueFerry to one of their OpenRC runlevels."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    runlevels = Path(config_home) / "rc" / "runlevels"
    try:
        return any(
            (level / BACKEND_SERVICE).is_symlink() or (level / BACKEND_SERVICE).exists()
            for level in runlevels.iterdir()
        )
    except OSError:
        return False


class UserServiceManager(Protocol):
    name: str

    def reload(self, *, timeout: float) -> None:
        """Make the manager notice changed service definitions."""

    def control(self, action: str, service: str, *, timeout: float) -> None:
        """Run ``start``, ``restart``, or ``stop`` for a user service."""

    def is_active(self, service: str, *, timeout: float) -> bool:
        """Return whether this manager currently runs the user service."""

    def try_restart(self, service: str, *, wait: bool) -> None:
        """Restart the user service only if it is running."""

    def manual_restart_hint(self, label: str) -> str | None:
        """Explain how to restart an unmanaged service, or None if managed."""


_ACTIONS = frozenset({"start", "restart", "stop"})


def _validated(action: str) -> str:
    if action not in _ACTIONS:
        raise ValueError(f"unsupported service action: {action}")
    return action


class SystemdUserManager:
    name = SYSTEMD

    def __init__(self, run: RunCommand) -> None:
        self._run = run

    def reload(self, *, timeout: float) -> None:
        self._run([SYSTEMCTL, "--user", "daemon-reload"], timeout=timeout)

    def control(self, action: str, service: str, *, timeout: float) -> None:
        self._run(
            [SYSTEMCTL, "--user", _validated(action), f"{service}.service"],
            timeout=timeout,
        )

    def is_active(self, service: str, *, timeout: float) -> bool:
        try:
            result = self._run(
                [SYSTEMCTL, "--user", "is-active", "--quiet", f"{service}.service"],
                timeout=timeout,
                check=False,
            )
        except CommandError:
            return False
        return result.returncode == 0

    def try_restart(self, service: str, *, wait: bool) -> None:
        command = [SYSTEMCTL, "--user"]
        if not wait:
            command.append("--no-block")
        command.extend(["try-restart", f"{service}.service"])
        self._run(command, timeout=30 if wait else 5)

    def manual_restart_hint(self, label: str) -> str | None:
        return None


class OpenRCUserManager:
    """OpenRC user services (``rc-service --user``, OpenRC 0.62+)."""

    name = OPENRC

    def __init__(self, run: RunCommand, rc_service: str) -> None:
        self._run = run
        self._rc_service = rc_service

    def reload(self, *, timeout: float) -> None:
        # openrc-run reads the init script on every invocation.
        return None

    def control(self, action: str, service: str, *, timeout: float) -> None:
        self._run(
            [self._rc_service, "--user", service, _validated(action)],
            timeout=timeout,
        )

    def is_active(self, service: str, *, timeout: float) -> bool:
        try:
            result = self._run(
                [self._rc_service, "--user", service, "status"],
                timeout=timeout,
                check=False,
            )
        except CommandError:
            return False
        return result.returncode == 0

    def try_restart(self, service: str, *, wait: bool) -> None:
        if not wait:
            # rc-service cannot queue a job; never block the daemon's main loop.
            raise ServiceManagerUnavailableError(
                f"{service} runs as an OpenRC user service; run "
                f"'rc-service --user {service} restart' to apply the change"
            )
        self._run(
            [self._rc_service, "--user", "--ifstarted", service, "restart"],
            timeout=30,
        )

    def manual_restart_hint(self, label: str) -> str | None:
        return (
            f"{label} is not running as an OpenRC user service; restart {label} "
            "(or log out and back in) to apply the change"
        )


# Linux 5.3+ with Python 3.9+; absent elsewhere, where kill() is the fallback.
_PIDFD_OPEN: Callable[[int], int] | None = getattr(os, "pidfd_open", None)
_PIDFD_SEND_SIGNAL: Callable[[int, int], None] | None = getattr(
    signal, "pidfd_send_signal", None,
)


class _AlreadyGone(Exception):
    """The name owner disappeared while it was being identified or signalled."""


class BusActivatedServices:
    """Lifecycle through the session bus when no user service manager runs it.

    Starting is D-Bus activation. Stopping signals the process that owns
    BlueFerry's bus name, identified only by the bus daemon and only if it
    runs as this user: SIGTERM, then SIGKILL after the same grace period as
    the systemd unit and the OpenRC script.
    """

    name = BUS_ACTIVATED

    def __init__(
        self,
        *,
        bus: Callable[[], Any] | None = None,
        bus_name: str = BUS_NAME,
        kill: Callable[[int, int], None] = os.kill,
        pidfd_open: Callable[[int], int] | None = _PIDFD_OPEN,
        pidfd_send_signal: Callable[[int, int], None] | None = _PIDFD_SEND_SIGNAL,
        close: Callable[[int], None] = os.close,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout: float = BACKEND_STOP_TIMEOUT_SECONDS,
        kill_wait: float = BACKEND_KILL_WAIT_SECONDS,
    ) -> None:
        self._bus = bus
        self._bus_name = bus_name
        self._kill = kill
        self._pidfd_open = pidfd_open
        self._pidfd_send_signal = pidfd_send_signal
        self._close = close
        self._clock = clock
        self._sleep = sleep
        self._stop_timeout = stop_timeout
        self._kill_wait = kill_wait

    def _session_bus(self) -> Any:
        if self._bus is not None:
            return self._bus()
        from blueferry.bus import get_session_bus

        return get_session_bus()

    @staticmethod
    def _unavailable(action: str, service: str) -> ServiceManagerUnavailableError:
        return ServiceManagerUnavailableError(
            f"Cannot {action} {service}: no service manager runs it; "
            f"{action} {service} manually"
        )

    def _owner(self, driver: Any) -> str | None:
        import dbus.exceptions

        try:
            return str(driver.GetNameOwner(self._bus_name))
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() == _NAME_HAS_NO_OWNER:
                return None
            raise

    def _owner_pid(self, driver: Any, owner: str) -> int:
        """Return the owner's PID after checking it is ours and plausible."""
        import dbus.exceptions

        try:
            uid = int(driver.GetConnectionUnixUser(owner))
            pid = int(driver.GetConnectionUnixProcessID(owner))
        except dbus.exceptions.DBusException as error:
            if error.get_dbus_name() == _NAME_HAS_NO_OWNER:
                raise _AlreadyGone from error
            raise
        if uid != os.getuid():
            raise ServiceManagerUnavailableError(
                f"{self._bus_name} is owned by another user; not stopping it"
            )
        if pid <= 1 or pid == os.getpid():
            raise ServiceManagerUnavailableError(
                f"the bus reported an invalid process for {self._bus_name}"
            )
        return pid

    def _signal(self, driver: Any, owner: str, pid: int, signum: int) -> None:
        """Signal ``pid`` only while it still owns the name, via a pidfd if possible.

        Holding a pidfd while re-checking the owner closes the window in which
        the PID could be reused by an unrelated process.
        """
        fd: int | None = None
        if self._pidfd_open is not None and self._pidfd_send_signal is not None:
            try:
                fd = self._pidfd_open(pid)
            except ProcessLookupError as error:
                raise _AlreadyGone from error
            except OSError:
                fd = None  # Kernel without pidfd support: fall back to kill().
        try:
            if self._owner(driver) != owner or self._owner_pid(driver, owner) != pid:
                raise _AlreadyGone
            if fd is not None and self._pidfd_send_signal is not None:
                self._pidfd_send_signal(fd, signum)
            else:
                self._kill(pid, signum)
        except ProcessLookupError as error:
            raise _AlreadyGone from error
        finally:
            if fd is not None:
                self._close(fd)

    def _released(self, driver: Any, owner: str, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while self._owner(driver) == owner:
            if self._clock() >= deadline:
                return False
            self._sleep(0.25)
        return True

    def _stop(self) -> None:
        import dbus

        bus = self._session_bus()
        driver = dbus.Interface(
            bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "org.freedesktop.DBus",
        )
        owner = self._owner(driver)
        if owner is None:
            return
        try:
            pid = self._owner_pid(driver, owner)
            self._signal(driver, owner, pid, signal.SIGTERM)
            if self._released(driver, owner, self._stop_timeout):
                return
            log.warning(
                "owner of %s (pid %d) ignored SIGTERM for %g seconds; sending SIGKILL",
                self._bus_name, pid, self._stop_timeout,
            )
            self._signal(driver, owner, pid, signal.SIGKILL)
            if self._released(driver, owner, self._kill_wait):
                return
        except _AlreadyGone:
            return
        raise ServiceManagerUnavailableError(
            f"owner of {self._bus_name} (pid {pid}) did not exit"
        )

    def _start(self, timeout: float) -> None:
        import dbus

        proxy = self._session_bus().get_object(self._bus_name, OBJECT_PATH)
        dbus.Interface(proxy, MESSAGES_IFACE).GetStatus(timeout=timeout)

    def reload(self, *, timeout: float) -> None:
        return None

    def control(self, action: str, service: str, *, timeout: float) -> None:
        _validated(action)
        if service != BACKEND_SERVICE:
            raise self._unavailable(action, service)
        import dbus.exceptions

        try:
            if action in {"stop", "restart"}:
                self._stop()
            if action in {"start", "restart"}:
                self._start(timeout)
        except dbus.exceptions.DBusException as error:
            raise ServiceManagerUnavailableError(
                error.get_dbus_message() or error.get_dbus_name() or str(error)
            ) from error

    def is_active(self, service: str, *, timeout: float) -> bool:
        return False

    def try_restart(self, service: str, *, wait: bool) -> None:
        raise self._unavailable("restart", service)

    def manual_restart_hint(self, label: str) -> str | None:
        return f"restart {label} (or log out and back in) to apply the change"


def user_service_manager(run: RunCommand) -> UserServiceManager:
    """Return the manager for session services such as WirePlumber."""
    kind = init_system()
    if kind == SYSTEMD:
        return SystemdUserManager(run)
    if kind == OPENRC and openrc_user_session():
        rc_service = rc_service_path()
        if rc_service is not None:
            return OpenRCUserManager(run, rc_service)
    return BusActivatedServices()


def backend_service_manager(run: RunCommand) -> UserServiceManager:
    """Return the manager that runs BlueFerry's own daemon.

    OpenRC manages it only when the user runs or enabled the BlueFerry user
    service and this desktop uses the bus that service is bound to; otherwise
    the session bus activates it.
    """
    manager = user_service_manager(run)
    if not isinstance(manager, OpenRCUserManager):
        return manager
    if not (openrc_backend_enabled() or manager.is_active(BACKEND_SERVICE, timeout=5)):
        return BusActivatedServices()
    if not openrc_session_bus():
        # The service would run on a different bus than this desktop's, so
        # rc-service could neither stop nor replace the daemon clients use.
        log.warning(
            "blueferry OpenRC user service is enabled but this desktop's "
            "session bus is not $XDG_RUNTIME_DIR/bus; using D-Bus activation"
        )
        return BusActivatedServices()
    return manager
