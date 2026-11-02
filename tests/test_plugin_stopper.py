"""Stopping a plugin that runs from an old venv; fake bus, fake /proc, no signals."""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from blueferry.plugin_stopper import (
    FAILED,
    FOREIGN,
    NOT_RUNNING,
    STILL_RUNNING,
    STOPPED,
    UNMANAGED,
    BusPluginStopper,
    StopOutcome,
    stop_note,
    stop_ok,
)

BUS = "io.weirdware.BlueFerry.Plugin.io.example.demo"
PID = 4242


class FakeDriver:
    def __init__(self, *, uid: int = 1000, pid: int = PID, exits: bool = True) -> None:
        self.owners = {BUS: ":1.7"}
        self.uid, self.pid, self.exits = uid, pid, exits

    def owner(self, name):
        return self.owners.get(name)

    def unix_user(self, owner):
        return self.uid if owner in self.owners.values() else None

    def process_id(self, owner):
        return self.pid if owner in self.owners.values() else None


def _proc(tmp_path: Path, argv: list[str], exe: str | None = None) -> Path:
    proc = tmp_path / "proc"
    directory = proc / str(PID)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    if exe is not None:
        os.symlink(exe, directory / "exe")
    return proc


def _stopper(driver: FakeDriver, proc: Path, signals: list, *, wait: float = 3.0,
             pidfd: bool = False) -> BusPluginStopper:
    now = [0.0]

    def kill(pid, signum):
        signals.append((pid, signum))
        if driver.exits:
            driver.owners.clear()

    def send(fd, signum):
        assert fd == 99
        kill(PID, signum)

    return BusPluginStopper(
        driver=lambda: driver, proc=proc, uid=lambda: 1000, kill=kill,
        pidfd_open=(lambda pid: 99) if pidfd else None,
        pidfd_send_signal=send if pidfd else None, close=lambda fd: None,
        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s), wait=wait,
    )


def test_console_script_from_the_old_venv_gets_sigterm(tmp_path) -> None:
    venv = tmp_path / "venvs" / "io.example.demo-aaaa"
    proc = _proc(tmp_path, [str(venv / "bin" / "python"), str(venv / "bin" / "demo"), "serve"],
                 exe="/usr/bin/python3.13")
    signals: list = []
    outcome = _stopper(FakeDriver(), proc, signals)(BUS, [venv])
    assert outcome == StopOutcome(STOPPED, PID) and signals == [(PID, signal.SIGTERM)]


def test_pidfd_is_used_when_available_and_deleted_exe_still_matches(tmp_path) -> None:
    venv = tmp_path / "venv"
    proc = _proc(tmp_path, ["demo"], exe=f"{venv}/bin/demo (deleted)")
    signals: list = []
    assert _stopper(FakeDriver(), proc, signals, pidfd=True)(BUS, [venv]).state == STOPPED
    assert signals == [(PID, signal.SIGTERM)]


@pytest.mark.parametrize("argv", [
    ["/usr/bin/python3", "-m", "demo", "/old/venv/bin/x"],   # path only as data
    ["/home/me/dev/demo/.venv/bin/python", "-m", "demo"],   # a developer's checkout
    ["/old/venv-other/bin/python"],                          # prefix but not inside
])
def test_a_process_from_elsewhere_is_left_alone(tmp_path, argv) -> None:
    proc = _proc(tmp_path, argv, exe="/usr/bin/python3")
    signals: list = []
    outcome = _stopper(FakeDriver(), proc, signals)(BUS, [Path("/old/venv")])
    assert outcome.state == FOREIGN and signals == []


def test_another_users_process_is_left_alone(tmp_path) -> None:
    venv = tmp_path / "venv"
    proc = _proc(tmp_path, [str(venv / "bin" / "python")])
    signals: list = []
    assert _stopper(FakeDriver(uid=0), proc, signals)(BUS, [venv]).state == FOREIGN
    assert signals == []


# Fixed ids: a pid in the id differs between pytest-xdist workers.
@pytest.mark.parametrize("pid", [os.getpid(), 1, 0], ids=["self", "init", "zero"])
def test_blueferry_itself_and_init_are_never_signalled(tmp_path, pid) -> None:
    """Even when the bus says so and /proc looks like the venv: a client that
    owns the name by mistake, or a bogus pid, is never sent SIGTERM."""
    venv = tmp_path / "venv"
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(str(venv / "bin" / "python").encode() + b"\0")
    signals: list = []
    outcome = _stopper(FakeDriver(pid=pid), proc, signals)(BUS, [venv])
    assert outcome.state == FOREIGN and signals == []


def test_no_owner_and_no_venvs_mean_nothing_to_do(tmp_path) -> None:
    driver = FakeDriver()
    driver.owners.clear()
    signals: list = []
    assert _stopper(driver, tmp_path, signals)(BUS, [tmp_path]).state == NOT_RUNNING
    assert _stopper(FakeDriver(), tmp_path, signals)(BUS, []).state == NOT_RUNNING
    assert signals == []


def test_a_process_ignoring_sigterm_is_reported_not_killed(tmp_path) -> None:
    venv = tmp_path / "venv"
    proc = _proc(tmp_path, [str(venv / "bin" / "demo")])
    signals: list = []
    outcome = _stopper(FakeDriver(exits=False), proc, signals, wait=1.0)(BUS, [venv])
    assert outcome == StopOutcome(STILL_RUNNING, PID)
    assert signals == [(PID, signal.SIGTERM)]  # never SIGKILL
    assert not stop_ok(outcome) and "process 4242" in stop_note(outcome)


def test_bus_errors_never_fail_the_install(tmp_path) -> None:
    def broken():
        raise RuntimeError("no session bus")

    outcome = BusPluginStopper(driver=broken)(BUS, [tmp_path])
    assert outcome.state == FAILED and outcome.reason == "RuntimeError"
    assert "RuntimeError" in stop_note(outcome)


def test_notes() -> None:
    assert stop_note(None) == "" and stop_note(StopOutcome(NOT_RUNNING)) == ""
    assert stop_note(StopOutcome(STOPPED)).endswith("restarts on next use.")
    assert stop_note(StopOutcome(STOPPED), restarts=False) == "Stopped the running plugin."
    assert "left running" in stop_note(StopOutcome(FOREIGN))
    assert stop_ok(None) and stop_ok(StopOutcome(STOPPED)) and not stop_ok(StopOutcome(FOREIGN))
    assert stop_ok(StopOutcome(UNMANAGED)) and "does not stop it" in stop_note(
        StopOutcome(UNMANAGED))


@pytest.mark.private_dbus
def test_driver_reads_owner_uid_and_pid_from_the_test_bus() -> None:
    from blueferry.plugin_stopper import DBusPythonDriver
    from tests.private_bus import open_private_bus

    owner_bus, query_bus = open_private_bus(), open_private_bus()
    try:
        owner_bus.request_name(BUS)
        driver = DBusPythonDriver(query_bus)
        owner = driver.owner(BUS)
        assert owner == owner_bus.get_unique_name()
        assert driver.unix_user(owner) == os.getuid()
        assert driver.process_id(owner) == os.getpid()
        assert driver.owner(BUS + ".missing") is None
        # Our own process owns the name: the stopper refuses to signal itself.
        stopper = BusPluginStopper(driver=lambda: driver, kill=lambda *_: pytest.fail("kill"),
                                   pidfd_open=None)
        assert stopper(BUS, [Path(os.path.dirname(os.__file__))]).state == FOREIGN
    finally:
        owner_bus.close()
        query_bus.close()
