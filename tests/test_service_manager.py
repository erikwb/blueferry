"""Init-system detection and user-service lifecycle, without running any."""
from __future__ import annotations

import signal
from types import SimpleNamespace

import dbus.exceptions
import pytest

from blueferry import backend_lifecycle, pair_setup, service_manager
from blueferry.errors import CommandError, PairingError
from blueferry.protocol import BUS_NAME, MESSAGES_API_VERSION


def _tool(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text("")
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    ("systemd_dir", "openrc_dir", "has_systemctl", "has_rc_service", "expected"),
    [
        (True, False, True, False, service_manager.SYSTEMD),
        # A marker without its tool is not a usable service manager.
        (True, False, False, True, service_manager.NO_SERVICE_MANAGER),
        # systemd booted, but systemctl lives elsewhere (NixOS keeps it under
        # /run/current-system/sw/bin). Main failed on /usr/bin/systemctl
        # here; this deliberately takes the session-bus path instead.
        (True, False, False, False, service_manager.NO_SERVICE_MANAGER),
        (False, True, False, True, service_manager.OPENRC),
        (False, True, False, False, service_manager.NO_SERVICE_MANAGER),
        # Containers and chroots keep issuing the systemctl commands they
        # issued before init-system detection existed.
        (False, False, True, False, service_manager.SYSTEMD),
        (False, False, False, True, service_manager.NO_SERVICE_MANAGER),
        (False, False, False, False, service_manager.NO_SERVICE_MANAGER),
    ],
)
def test_init_system_detection(
    tmp_path, systemd_dir, openrc_dir, has_systemctl, has_rc_service, expected,
):
    systemd_runtime = tmp_path / "run-systemd-system"
    openrc_runtime = tmp_path / "run-openrc"
    if systemd_dir:
        systemd_runtime.mkdir()
    if openrc_dir:
        openrc_runtime.mkdir()
    systemctl = _tool(tmp_path, "systemctl") if has_systemctl else str(tmp_path / "none")
    rc_service = (
        _tool(tmp_path, "rc-service") if has_rc_service else str(tmp_path / "absent")
    )

    assert service_manager.detect_init_system(
        systemd_runtime=systemd_runtime,
        openrc_runtime=openrc_runtime,
        systemctl=systemctl,
        rc_service_candidates=(str(tmp_path / "missing"), rc_service),
    ) == expected


def test_rc_service_must_be_executable(tmp_path):
    plain = tmp_path / "rc-service"
    plain.write_text("")
    plain.chmod(0o644)

    assert service_manager.rc_service_path((str(plain),)) is None


def _recording(returncode: int = 0):
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    return calls, run


def test_systemd_user_commands_are_unchanged():
    calls, run = _recording()
    manager = service_manager.SystemdUserManager(run)

    manager.reload(timeout=20)
    manager.control("restart", "blueferry", timeout=45)
    assert manager.is_active("wireplumber", timeout=5) is True
    manager.try_restart("wireplumber", wait=False)

    assert [argv for argv, _kwargs in calls] == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "restart", "blueferry.service"],
        ["/usr/bin/systemctl", "--user", "is-active", "--quiet", "wireplumber.service"],
        ["/usr/bin/systemctl", "--user", "--no-block", "try-restart", "wireplumber.service"],
    ]
    assert manager.manual_restart_hint("WirePlumber") is None


def test_openrc_user_commands():
    calls, run = _recording()
    manager = service_manager.OpenRCUserManager(run, "/sbin/rc-service")

    manager.reload(timeout=20)
    manager.control("start", "blueferry", timeout=45)
    manager.control("stop", "blueferry", timeout=30)
    assert manager.is_active("wireplumber", timeout=5) is True
    manager.try_restart("wireplumber", wait=True)

    assert calls == [
        (["/sbin/rc-service", "--user", "blueferry", "start"], {"timeout": 45}),
        (["/sbin/rc-service", "--user", "blueferry", "stop"], {"timeout": 30}),
        (
            ["/sbin/rc-service", "--user", "wireplumber", "status"],
            {"timeout": 5, "check": False},
        ),
        (
            ["/sbin/rc-service", "--user", "--ifstarted", "wireplumber", "restart"],
            {"timeout": 30},
        ),
    ]


def test_openrc_never_blocks_for_a_background_restart():
    calls, run = _recording()
    manager = service_manager.OpenRCUserManager(run, "/sbin/rc-service")

    with pytest.raises(service_manager.ServiceManagerUnavailableError, match="rc-service"):
        manager.try_restart("wireplumber", wait=False)
    assert calls == []


def test_openrc_reports_stopped_and_unrunnable_services_as_inactive():
    _calls, run = _recording(returncode=3)
    manager = service_manager.OpenRCUserManager(run, "/sbin/rc-service")
    assert manager.is_active("wireplumber", timeout=5) is False

    def missing(argv, **_kwargs):
        raise CommandError(tuple(argv), "rc-service is not installed")

    manager = service_manager.OpenRCUserManager(missing, "/sbin/rc-service")
    assert manager.is_active("wireplumber", timeout=5) is False


def test_service_actions_are_restricted():
    calls, run = _recording()
    for manager in (
        service_manager.SystemdUserManager(run),
        service_manager.OpenRCUserManager(run, "/sbin/rc-service"),
        service_manager.BusActivatedServices(bus=lambda: None),
    ):
        with pytest.raises(ValueError, match="unsupported service action"):
            manager.control("enable", "blueferry", timeout=30)
    assert calls == []


def test_bluetooth_restart_hint_follows_init_system(monkeypatch):
    assert service_manager.bluetooth_restart_command() == (
        "sudo systemctl restart bluetooth.service"
    )
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    assert service_manager.bluetooth_restart_command() == "sudo rc-service bluetooth restart"
    monkeypatch.setattr(
        service_manager, "init_system", lambda: service_manager.NO_SERVICE_MANAGER,
    )
    assert service_manager.bluetooth_restart_command() is None


# ---- backend selection ----------------------------------------------------


@pytest.fixture
def openrc_host(monkeypatch, tmp_path):
    """An OpenRC system; tests decide whether a user session exists."""
    rc_service = _tool(tmp_path, "rc-service")
    monkeypatch.setattr(service_manager, "RC_SERVICE_CANDIDATES", (rc_service,))
    monkeypatch.setattr(service_manager, "init_system", lambda: service_manager.OPENRC)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_home = tmp_path / "config"
    (config_home / "rc" / "runlevels" / "default").mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    def user_session(bus=f"unix:path={runtime}/bus"):
        (runtime / "openrc").mkdir()
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus)

    return SimpleNamespace(
        rc_service=rc_service,
        user_session=user_session,
        enable=lambda: (config_home / "rc/runlevels/default/blueferry").symlink_to(
            "/etc/user/init.d/blueferry"
        ),
    )


def test_openrc_without_a_user_session_uses_bus_activation(openrc_host):
    calls, run = _recording()

    for factory in (
        service_manager.user_service_manager,
        service_manager.backend_service_manager,
    ):
        assert isinstance(factory(run), service_manager.BusActivatedServices)
    assert calls == []


def test_openrc_session_without_the_blueferry_service_uses_bus_activation(openrc_host):
    openrc_host.user_session()
    calls, run = _recording(returncode=1)  # "service `blueferry' does not exist"

    assert isinstance(
        service_manager.user_service_manager(run), service_manager.OpenRCUserManager,
    )
    assert isinstance(
        service_manager.backend_service_manager(run),
        service_manager.BusActivatedServices,
    )
    assert calls == [
        (
            [openrc_host.rc_service, "--user", "blueferry", "status"],
            {"timeout": 5, "check": False},
        ),
    ]


def test_started_openrc_service_manages_the_backend(openrc_host):
    openrc_host.user_session()
    _calls, run = _recording(returncode=0)

    assert isinstance(
        service_manager.backend_service_manager(run), service_manager.OpenRCUserManager,
    )


def test_enabled_but_stopped_openrc_service_still_manages_the_backend(openrc_host):
    # Forgetting a phone stops the service; re-pairing must start it again
    # rather than bus-activate an unsupervised daemon beside it.
    openrc_host.user_session()
    openrc_host.enable()
    calls, run = _recording(returncode=3)

    assert isinstance(
        service_manager.backend_service_manager(run), service_manager.OpenRCUserManager,
    )
    assert calls == []


@pytest.mark.parametrize(
    "address",
    [
        "unix:path=/tmp/dbus-AbCdEf,guid=0123",  # dbus-run-session desktop
        "unix:path={runtime}/bus-other",
        "",
    ],
)
def test_openrc_service_on_another_bus_is_not_trusted_for_the_backend(
    openrc_host, tmp_path, caplog, address,
):
    openrc_host.user_session(address.format(runtime=tmp_path / "runtime"))
    openrc_host.enable()
    _calls, run = _recording(returncode=0)

    with caplog.at_level("WARNING", logger="blueferry.service_manager"):
        manager = service_manager.backend_service_manager(run)

    assert isinstance(manager, service_manager.BusActivatedServices)
    assert "session bus is not $XDG_RUNTIME_DIR/bus" in caplog.text


def test_openrc_session_bus_address_may_carry_a_guid(openrc_host, tmp_path):
    openrc_host.user_session(f"unix:path={tmp_path / 'runtime'}/bus,guid=0123")
    openrc_host.enable()
    _calls, run = _recording()

    assert isinstance(
        service_manager.backend_service_manager(run), service_manager.OpenRCUserManager,
    )


def test_openrc_backend_upgrade_restart_skips_daemon_reload(monkeypatch, openrc_host):
    openrc_host.user_session()
    openrc_host.enable()
    monkeypatch.setattr(backend_lifecycle, "installed_build_sha", lambda: None)
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: "current")
    calls = []
    monkeypatch.setattr(
        backend_lifecycle, "run_command", lambda args, **_kwargs: calls.append(args),
    )
    statuses = iter([
        {"backend_release": "old"},
        {"backend_release": "old"},
        {"backend_release": "current", "api_version": MESSAGES_API_VERSION},
    ])

    backend_lifecycle.ensure_backend_current(lambda: next(statuses))

    assert calls == [[openrc_host.rc_service, "--user", "blueferry", "restart"]]


def test_openrc_pairing_handoff_restarts_the_user_service(monkeypatch, openrc_host):
    openrc_host.user_session()
    openrc_host.enable()
    calls = []
    monkeypatch.setattr(
        pair_setup, "run_command", lambda args, **_kwargs: calls.append(args),
    )

    pair_setup._restart_user_service()
    pair_setup._stop_user_service()

    assert calls == [
        [openrc_host.rc_service, "--user", "blueferry", "restart"],
        [openrc_host.rc_service, "--user", "blueferry", "stop"],
    ]


def test_systemd_pairing_failure_names_the_failed_command(monkeypatch):
    def fail(argv, **_kwargs):
        raise CommandError(tuple(argv), "Unit blueferry.service not found.")

    monkeypatch.setattr(pair_setup, "run_command", fail)

    with pytest.raises(PairingError) as failure:
        pair_setup._restart_user_service()

    assert str(failure.value) == (
        "Could not run /usr/bin/systemctl --user daemon-reload: "
        "Unit blueferry.service not found."
    )


# ---- D-Bus activation lifecycle -------------------------------------------


def _no_owner():
    return dbus.exceptions.DBusException(
        "no owner", name="org.freedesktop.DBus.Error.NameHasNoOwner",
    )


class _Proxy:
    def __init__(self, target):
        self._target = target

    def get_dbus_method(self, member, _interface=None):
        return getattr(self._target, member)


class _SessionBus:
    """The bus daemon's view of one BlueFerry owner that exits on SIGTERM."""

    def __init__(self, *, uid: int, pid: int = 4242, running: bool = True):
        self.uid = uid
        self.pid = pid
        self.owner = ":1.42" if running else None
        self.status_calls = 0
        self.status_timeouts = []
        self.lookups = []

    def get_object(self, name, _path):
        if name == "org.freedesktop.DBus":
            return _Proxy(self)
        assert name == BUS_NAME
        return _Proxy(self)

    def GetNameOwner(self, name):
        self.lookups.append(name)
        if self.owner is None:
            raise _no_owner()
        return self.owner

    def GetConnectionUnixUser(self, owner):
        assert owner == self.owner
        return self.uid

    def GetConnectionUnixProcessID(self, owner):
        assert owner == self.owner
        return self.pid

    def GetStatus(self, timeout):
        self.status_calls += 1
        self.status_timeouts.append(timeout)
        self.owner = ":1.43"
        return "{}"


_REAL_BUS_ACTIVATED = service_manager.BusActivatedServices


def _bus_services(bus, kills, *, exits_on=(signal.SIGTERM,), **kwargs):
    def kill(pid, sig):
        kills.append((pid, sig))
        if sig in exits_on:
            bus.owner = None

    kwargs.setdefault("pidfd_open", None)
    kwargs.setdefault("sleep", lambda _s: None)
    return _REAL_BUS_ACTIVATED(bus=lambda: bus, kill=kill, **kwargs)


def test_bus_activated_restart_terminates_the_owner_and_reactivates():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []

    _bus_services(bus, kills).control("restart", "blueferry", timeout=30)

    assert kills == [(4242, signal.SIGTERM)]
    assert bus.status_calls == 1
    assert set(bus.lookups) == {BUS_NAME}


def test_bus_activated_stop_without_a_running_daemon_is_a_no_op():
    bus = _SessionBus(uid=0, running=False)
    kills = []

    _bus_services(bus, kills).control("stop", "blueferry", timeout=30)

    assert kills == []
    assert bus.status_calls == 0


def test_bus_activated_stop_refuses_another_users_process():
    import os

    bus = _SessionBus(uid=os.getuid() + 1)
    kills = []

    with pytest.raises(service_manager.ServiceManagerUnavailableError, match="another user"):
        _bus_services(bus, kills).control("stop", "blueferry", timeout=30)
    assert kills == []


@pytest.mark.parametrize("pid", [0, 1])
def test_bus_activated_stop_refuses_implausible_pids(pid):
    import os

    bus = _SessionBus(uid=os.getuid(), pid=pid)
    kills = []

    with pytest.raises(service_manager.ServiceManagerUnavailableError):
        _bus_services(bus, kills).control("stop", "blueferry", timeout=30)
    assert kills == []


def _fake_clock():
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    return now, sleep


def test_bus_activated_stop_escalates_to_sigkill_after_the_grace_period(caplog):
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()
    services = _bus_services(
        bus, kills, exits_on=(signal.SIGKILL,),
        clock=lambda: now[0], sleep=sleep, stop_timeout=180,
    )

    with caplog.at_level("WARNING", logger="blueferry.service_manager"):
        services.control("stop", "blueferry", timeout=200)

    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert 180 <= now[0] < 181
    assert "sending SIGKILL" in caplog.text


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_bus_activated_stop_never_outlives_the_callers_timeout(action):
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()
    services = _bus_services(
        bus, kills, exits_on=(),
        clock=lambda: now[0], sleep=sleep, stop_timeout=180,
    )

    with pytest.raises(
        service_manager.ServiceManagerUnavailableError,
        match="still shutting down 30 seconds after SIGTERM",
    ):
        services.control(action, "blueferry", timeout=30)

    # Like a timed-out systemctl: SIGTERM stays in effect so an in-flight
    # Bluetooth operation can finish, but nothing escalates to SIGKILL
    # before the unit's grace period and nothing reactivates the old owner.
    assert kills == [(4242, signal.SIGTERM)]
    assert now[0] == pytest.approx(30)
    assert bus.status_calls == 0


def test_bus_activated_restart_activates_within_the_remaining_time():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()

    def kill(pid, sig):
        kills.append((pid, sig))
        now[0] += 12  # The old daemon takes 12 seconds to release the name.
        bus.owner = None

    services = _REAL_BUS_ACTIVATED(
        bus=lambda: bus, kill=kill, pidfd_open=None,
        clock=lambda: now[0], sleep=sleep,
    )

    services.control("restart", "blueferry", timeout=30)

    assert bus.status_timeouts == [pytest.approx(18)]


def test_bus_activated_kill_wait_is_capped_by_the_callers_timeout():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()
    services = _bus_services(
        bus, kills, exits_on=(),
        clock=lambda: now[0], sleep=sleep, stop_timeout=2, kill_wait=5,
    )

    with pytest.raises(service_manager.ServiceManagerUnavailableError, match="did not exit"):
        services.control("stop", "blueferry", timeout=4)

    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert now[0] == pytest.approx(4)


def test_bus_activated_stop_fails_if_the_owner_survives_sigkill():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()
    services = _bus_services(
        bus, kills, exits_on=(),
        clock=lambda: now[0], sleep=sleep, stop_timeout=2, kill_wait=5,
    )

    with pytest.raises(
        service_manager.ServiceManagerUnavailableError,
        match=r"owner of io\.weirdware\.BlueFerry \(pid 4242\) did not exit",
    ):
        services.control("stop", "blueferry", timeout=30)
    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert now[0] >= 7


def test_bus_activated_stop_does_not_sigkill_a_replaced_owner():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    now, sleep = _fake_clock()

    def kill(pid, sig):
        kills.append((pid, sig))

    def slow_sleep(seconds):
        sleep(seconds)
        if now[0] > 1.5:
            # A new daemon took the name, but the old owner is still listed
            # until this point; the escalation must re-verify the owner.
            bus.owner = ":1.99"

    services = _REAL_BUS_ACTIVATED(
        bus=lambda: bus, kill=kill, pidfd_open=None,
        clock=lambda: now[0], sleep=slow_sleep, stop_timeout=2,
    )

    services.control("stop", "blueferry", timeout=30)

    assert kills == [(4242, signal.SIGTERM)]


def test_bus_activated_stop_uses_a_pidfd_and_rechecks_the_owner():
    import os

    bus = _SessionBus(uid=os.getuid())
    events = []

    def pidfd_open(pid):
        events.append(("open", pid, tuple(bus.lookups)))
        return 99

    def pidfd_send_signal(fd, sig):
        # The owner was looked up again after the pidfd was opened.
        assert len(bus.lookups) >= 2
        events.append(("signal", fd, sig))
        bus.owner = None

    services = _REAL_BUS_ACTIVATED(
        bus=lambda: bus,
        kill=lambda *_args: (_ for _ in ()).throw(AssertionError("used kill()")),
        pidfd_open=pidfd_open,
        pidfd_send_signal=pidfd_send_signal,
        close=lambda fd: events.append(("close", fd)),
        sleep=lambda _s: None,
    )

    services.control("stop", "blueferry", timeout=30)

    assert [event[0] for event in events] == ["open", "signal", "close"]
    assert events[1] == ("signal", 99, signal.SIGTERM)


def test_bus_activated_stop_falls_back_to_kill_without_pidfd_support():
    import errno
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []

    def unsupported(_pid):
        raise OSError(errno.ENOSYS, "Function not implemented")

    _bus_services(bus, kills, pidfd_open=unsupported, pidfd_send_signal=None).control(
        "stop", "blueferry", timeout=30,
    )

    assert kills == [(4242, signal.SIGTERM)]


def test_bus_activated_stop_treats_a_vanished_process_as_stopped():
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []

    def gone(_pid):
        raise ProcessLookupError

    _bus_services(bus, kills, pidfd_open=gone, pidfd_send_signal=lambda *_: None).control(
        "stop", "blueferry", timeout=30,
    )

    assert kills == []


@pytest.mark.parametrize("method", ["GetConnectionUnixUser", "GetConnectionUnixProcessID"])
def test_bus_activated_stop_treats_a_vanished_owner_as_stopped(method):
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []

    def vanished(_owner):
        raise _no_owner()

    setattr(bus, method, vanished)

    _bus_services(bus, kills).control("stop", "blueferry", timeout=30)

    assert kills == []


def test_bus_activated_stop_never_signals_itself():
    import os

    bus = _SessionBus(uid=os.getuid(), pid=os.getpid())
    kills = []

    with pytest.raises(service_manager.ServiceManagerUnavailableError, match="invalid"):
        _bus_services(bus, kills).control("stop", "blueferry", timeout=30)
    assert kills == []


def test_bus_activated_services_manage_only_blueferry():
    services = service_manager.BusActivatedServices(bus=lambda: None)

    assert services.is_active("wireplumber", timeout=5) is False
    with pytest.raises(service_manager.ServiceManagerUnavailableError):
        services.try_restart("wireplumber", wait=True)
    with pytest.raises(service_manager.ServiceManagerUnavailableError, match="manually"):
        services.control("restart", "wireplumber", timeout=30)
    assert "restart WirePlumber" in services.manual_restart_hint("WirePlumber")


def test_bus_activated_pairing_handoff_uses_the_session_bus(monkeypatch, openrc_host):
    import os

    bus = _SessionBus(uid=os.getuid())
    kills = []
    monkeypatch.setattr(
        service_manager,
        "BusActivatedServices",
        lambda: _bus_services(bus, kills),
    )
    monkeypatch.setattr(
        pair_setup,
        "run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran a command")),
    )

    pair_setup._restart_user_service()

    assert kills == [(4242, signal.SIGTERM)]
    assert bus.status_calls == 1


def test_bus_activation_failures_become_pairing_errors(monkeypatch, openrc_host):
    class FailingBus(_SessionBus):
        def GetStatus(self, timeout):
            raise dbus.exceptions.DBusException(
                "Failed to execute program", name="org.freedesktop.DBus.Error.Spawn.ExecFailed",
            )

    bus = FailingBus(uid=0, running=False)
    monkeypatch.setattr(
        service_manager, "BusActivatedServices", lambda: _bus_services(bus, []),
    )

    with pytest.raises(PairingError, match="Could not restart BlueFerry: Failed to execute"):
        pair_setup._restart_user_service()


def test_bus_activated_fallback_reports_the_original_status_failure(
    monkeypatch, openrc_host,
):
    monkeypatch.setattr(backend_lifecycle, "installed_build_sha", lambda: None)
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: None)
    monkeypatch.setattr(
        backend_lifecycle,
        "run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran a command")),
    )
    failures = iter([
        backend_lifecycle.BlueFerryError("activation failed: local.env is missing"),
        backend_lifecycle.BlueFerryError("second attempt"),
    ])

    def read_status():
        raise next(failures)

    with pytest.raises(
        backend_lifecycle.BackendLifecycleError, match=r"local\.env is missing",
    ):
        backend_lifecycle.ensure_backend_current(read_status)


def test_bus_activated_fallback_retries_status(monkeypatch, openrc_host):
    monkeypatch.setattr(backend_lifecycle, "installed_build_sha", lambda: None)
    monkeypatch.setattr(backend_lifecycle, "installed_release", lambda: None)
    statuses = iter([
        backend_lifecycle.BlueFerryError("bus has not seen the activation file"),
        {"daemon": True, "api_version": MESSAGES_API_VERSION},
    ])

    def read_status():
        value = next(statuses)
        if isinstance(value, Exception):
            raise value
        return value

    assert backend_lifecycle.ensure_backend_current(read_status)["daemon"] is True


# ---- real private bus -----------------------------------------------------


@pytest.mark.private_dbus
def test_bus_activated_stop_terminates_a_real_name_owner(tmp_path):
    import os
    import subprocess
    import sys
    import time

    import dbus

    name = f"{BUS_NAME}.StopTest{os.getpid()}"
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, dbus, dbus.mainloop.glib\n"
            "from gi.repository import GLib\n"
            "dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)\n"
            "bus = dbus.SessionBus()\n"
            "bus.request_name(sys.argv[1], dbus.bus.NAME_FLAG_DO_NOT_QUEUE)\n"
            "open(sys.argv[2], 'w').close()\n"
            "GLib.MainLoop().run()\n",
            name,
            str(ready),
        ],
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None, "name owner exited early"
            assert time.monotonic() < deadline, "name owner did not start"
            time.sleep(0.05)
        services = service_manager.BusActivatedServices(
            bus=dbus.SessionBus, bus_name=name, stop_timeout=10,
        )

        services.control("stop", "blueferry", timeout=5)

        assert child.wait(timeout=5) == -signal.SIGTERM
        assert not dbus.SessionBus().name_has_owner(name)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
