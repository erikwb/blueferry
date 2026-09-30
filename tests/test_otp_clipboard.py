"""Clipboard helper selection and ownership without touching a real clipboard."""
from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from blueferry import commands
from blueferry.errors import CommandError
from blueferry.otp_clipboard import (
    ClipboardTarget,
    ClipboardTicket,
    ClipboardWriter,
    _wayland_sockets,
    copy_argv,
    find_target,
    helper_environment,
    send_signal,
    supports_sensitive_hint,
)

CODE = "482913"
WAYLAND = ClipboardTarget(
    "wayland", "wl-copy", "/usr/bin/wl-copy", (("WAYLAND_DISPLAY", "wayland-0"),)
)


def _which(*available: str):
    return lambda tool: f"/usr/bin/{tool}" if tool in available else None


# ---- helper selection ------------------------------------------------------


def test_wayland_display_selects_wl_copy() -> None:
    target = find_target({"WAYLAND_DISPLAY": "wayland-0"}, which=_which("wl-copy", "xclip"))
    assert target == ClipboardTarget("wayland", "wl-copy", "/usr/bin/wl-copy")


def test_helper_paths_are_made_absolute() -> None:
    target = find_target({"WAYLAND_DISPLAY": "wayland-0"}, which=lambda _tool: "bin/wl-copy")
    assert target is not None
    assert target.executable == os.path.abspath("bin/wl-copy")


def test_single_runtime_socket_recovers_a_missing_wayland_display() -> None:
    target = find_target(
        {"XDG_RUNTIME_DIR": "/run/user/1000"},
        which=_which("wl-copy"),
        list_sockets=lambda _directory: ["wayland-1"],
    )
    assert target is not None
    assert target.env_overrides == (("WAYLAND_DISPLAY", "wayland-1"),)


def test_ambiguous_runtime_sockets_are_not_guessed() -> None:
    target = find_target(
        {"XDG_RUNTIME_DIR": "/run/user/1000"},
        which=_which("wl-copy"),
        list_sockets=lambda _directory: ["wayland-0", "wayland-1"],
    )
    assert target is None


def test_runtime_socket_scan_accepts_only_real_sockets(tmp_path) -> None:
    (tmp_path / "wayland-0").write_text("")
    (tmp_path / "wayland-0.lock").write_text("")
    assert _wayland_sockets(str(tmp_path)) == []
    assert _wayland_sockets(None) == []

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(tmp_path / "wayland-1"))
        assert _wayland_sockets(str(tmp_path)) == ["wayland-1"]
    finally:
        listener.close()


def test_x11_falls_back_to_xclip_then_xsel() -> None:
    assert find_target({"DISPLAY": ":0"}, which=_which("xclip", "xsel")).tool == "xclip"
    assert find_target({"DISPLAY": ":0"}, which=_which("xsel")).tool == "xsel"
    assert find_target({"DISPLAY": ":0"}, which=_which()) is None
    assert find_target({}, which=_which("xclip")) is None


def test_excluding_wl_copy_falls_back_to_x11() -> None:
    environ = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    which = _which("wl-copy", "xclip")
    assert find_target(environ, which=which).tool == "wl-copy"
    assert find_target(environ, which=which, exclude=frozenset({"wl-copy"})).tool == "xclip"


def test_defaults_resolve_at_call_time(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.otp_clipboard.shutil.which", lambda _tool: None)
    assert find_target({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}) is None


def test_helper_environment_is_allowlisted() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "LANG": "de_CH.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DISPLAY": ":0",
        "XAUTHORITY": "/run/user/1000/xauth",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "BLUEFERRY_MAC": "02:00:00:00:00:01",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh",
        "LD_PRELOAD": "/tmp/evil.so",
    }

    env = helper_environment(environ, WAYLAND)

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "LANG": "de_CH.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DISPLAY": ":0",
        "XAUTHORITY": "/run/user/1000/xauth",
        "WAYLAND_DISPLAY": "wayland-0",
    }


@pytest.mark.parametrize(
    ("tool", "sensitive", "expected"),
    [
        ("wl-copy", True, ("/usr/bin/wl-copy", "--foreground", "--type", "text/plain", "--sensitive")),
        ("wl-copy", False, ("/usr/bin/wl-copy", "--foreground", "--type", "text/plain")),
        ("xclip", False, ("/usr/bin/xclip", "-selection", "clipboard", "-quiet")),
        ("xsel", False, ("/usr/bin/xsel", "--clipboard", "--input", "--nodetach")),
    ],
)
def test_helper_argv_never_contains_the_code(tool, sensitive, expected) -> None:
    kind = "wayland" if tool == "wl-copy" else "x11"
    argv = copy_argv(ClipboardTarget(kind, tool, f"/usr/bin/{tool}"), sensitive=sensitive)
    assert argv == expected
    assert "--paste-once" not in argv


def test_sensitive_support_is_probed_from_help_text() -> None:
    def run(argv, **_kwargs):
        assert argv == ["/usr/bin/wl-copy", "--help"]
        return subprocess.CompletedProcess(argv, 0, "  --sensitive  Hint that", "")

    assert supports_sensitive_hint("/usr/bin/wl-copy", run=run) is True
    assert supports_sensitive_hint(
        "/usr/bin/wl-copy",
        run=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "-o, --paste-once", ""),
    ) is False

    def missing(argv, **_kwargs):
        raise CommandError(tuple(argv), "missing")

    assert supports_sensitive_hint("/usr/bin/wl-copy", run=missing) is False


def test_sensitive_probe_default_runner_resolves_at_call_time(monkeypatch) -> None:
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "--sensitive", "")

    monkeypatch.setattr(commands, "run_command", run)
    assert supports_sensitive_hint("/usr/bin/wl-copy") is True
    assert calls == [["/usr/bin/wl-copy", "--help"]]


# ---- writer lifecycle ------------------------------------------------------


class _Loop:
    """Fake GLib: timers, child watches, and a background worker."""

    def __init__(self) -> None:
        self.timers: dict[int, tuple[int, object]] = {}
        self.watches: dict[int, object] = {}
        self.jobs: list[tuple[object, object, object]] = []
        self.signals: list[tuple[int, int]] = []
        self.next_id = 1

    def schedule(self, delay_ms, callback) -> int:
        source = self.next_id
        self.next_id += 1
        self.timers[source] = (delay_ms, callback)
        return source

    def cancel(self, source) -> None:
        self.timers.pop(source, None)

    def fire_timers(self) -> None:
        for source, (_delay, callback) in list(self.timers.items()):
            self.timers.pop(source, None)
            callback()

    def watch(self, pid, callback) -> int:
        self.watches[pid] = callback
        return pid

    def exit(self, pid, status) -> None:
        self.watches.pop(pid)(pid, status)

    def submit(self, operation, *, on_success, on_error) -> None:
        self.jobs.append((operation, on_success, on_error))

    def run_jobs(self) -> None:
        while self.jobs:
            operation, on_success, on_error = self.jobs.pop(0)
            try:
                value = operation()
            except Exception as error:
                on_error(error)
            else:
                on_success(value)

    def signal(self, ticket, signum) -> None:
        self.signals.append((ticket.process.pid, signum))


_EXIT_OK = 0
_EXIT_ERROR = 1 << 8  # waitpid status for exit(1)
_KILLED_BY_TERM = signal.SIGTERM


def _writer(*, clear_after_s=0, sensitive=True, find=None):
    spawned = []
    loop = _Loop()
    probes = []

    def spawn(argv, *, stdin_text, env):
        process = SimpleNamespace(pid=1000 + len(spawned), returncode=None)
        spawned.append(SimpleNamespace(argv=argv, stdin=stdin_text, env=env, process=process))
        return process

    def probe(executable):
        probes.append(executable)
        return sensitive

    writer = ClipboardWriter(
        clear_after_s=clear_after_s,
        environ={"XDG_RUNTIME_DIR": "/run/user/1000", "SECRET_TOKEN": "x"},
        find=find or (lambda _environ, **_kwargs: WAYLAND),
        spawn=spawn,
        probe_sensitive=probe,
        submit_probe=loop.submit,
        schedule_ms=loop.schedule,
        cancel=loop.cancel,
        watch_child=loop.watch,
        signal_helper=loop.signal,
        open_pidfd=lambda _pid: None,
    )
    return writer, spawned, loop, probes


def test_probe_runs_on_the_worker_before_any_code_arrives() -> None:
    writer, spawned, loop, probes = _writer()

    writer.start_probe()
    assert probes == []  # queued, not run on the caller's thread
    loop.run_jobs()
    writer.copy(CODE)

    assert probes == ["/usr/bin/wl-copy"]
    assert "--sensitive" in spawned[0].argv


def test_unprobed_copy_does_not_wait_for_the_probe() -> None:
    writer, spawned, loop, probes = _writer()

    writer.copy(CODE)

    assert probes == []
    assert "--sensitive" not in spawned[0].argv
    loop.run_jobs()
    writer.copy(CODE)
    assert "--sensitive" in spawned[1].argv


def test_missing_sensitive_support_warns_only_after_the_probe(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer, _spawned, loop, _probes = _writer(sensitive=False)

    writer.start_probe()
    assert "cannot mark" not in caplog.text
    loop.run_jobs()

    assert "wl-copy cannot mark clipboard data as sensitive" in caplog.text


def test_code_travels_on_stdin_with_an_allowlisted_environment() -> None:
    writer, spawned, loop, _probes = _writer()
    writer.start_probe()
    loop.run_jobs()

    ticket = writer.copy(CODE)

    assert ticket is not None and ticket.tool == "wl-copy"
    [call] = spawned
    assert call.stdin == CODE
    assert CODE not in " ".join(call.argv)
    assert call.env == {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    assert loop.timers == {}
    assert writer.state(ticket) == "running"


def test_child_watch_reaps_and_reports_the_exit_status() -> None:
    writer, spawned, loop, _probes = _writer()
    ticket = writer.copy(CODE)

    loop.exit(spawned[0].process.pid, _EXIT_ERROR)

    assert writer.state(ticket) == "failed"
    # Popen must never wait on a child GLib already reaped.
    assert spawned[0].process.returncode == 1


def test_a_newer_copy_supersedes_the_older_ticket() -> None:
    writer, spawned, loop, _probes = _writer()
    first = writer.copy(CODE)
    second = writer.copy("135790")

    assert writer.state(first) == "superseded"
    assert writer.state(second) == "running"
    assert loop.signals == [(spawned[0].process.pid, signal.SIGTERM)]


def test_auto_clear_stops_a_helper_that_still_owns_the_code() -> None:
    writer, spawned, loop, _probes = _writer(clear_after_s=30)
    writer.copy(CODE)

    assert [delay for delay, _callback in loop.timers.values()] == [30_000]
    loop.fire_timers()

    assert loop.signals == [(spawned[0].process.pid, signal.SIGTERM)]


def test_auto_clear_leaves_a_replaced_clipboard_alone() -> None:
    writer, spawned, loop, _probes = _writer(clear_after_s=30)
    writer.copy(CODE)
    # Something else was copied: wl-copy exits on its own.
    loop.exit(spawned[0].process.pid, _EXIT_OK)

    loop.fire_timers()

    assert loop.signals == []


def test_release_escalates_to_sigkill_without_blocking() -> None:
    writer, spawned, loop, _probes = _writer()
    writer.copy(CODE)

    writer.release()

    pid = spawned[0].process.pid
    assert loop.signals == [(pid, signal.SIGTERM)]
    [(delay, _callback)] = loop.timers.values()
    assert delay == 1000
    loop.fire_timers()
    assert loop.signals == [(pid, signal.SIGTERM), (pid, signal.SIGKILL)]


def test_release_does_not_kill_a_helper_that_already_exited() -> None:
    writer, spawned, loop, _probes = _writer()
    writer.copy(CODE)
    writer.release()

    loop.exit(spawned[0].process.pid, _KILLED_BY_TERM)
    loop.fire_timers()

    assert loop.signals == [(spawned[0].process.pid, signal.SIGTERM)]


def test_missing_helper_warns_once_without_the_code(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer, _spawned, _loop, _probes = _writer(find=lambda _environ, **_kwargs: None)

    assert writer.copy(CODE) is None
    assert writer.copy(CODE) is None

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert CODE not in caplog.text


def test_helper_start_failure_is_logged_without_the_code(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    def spawn(argv, **_kwargs):
        raise CommandError(tuple(argv), f"{argv[0]} could not be started")

    loop = _Loop()
    writer = ClipboardWriter(
        environ={},
        find=lambda _environ, **_kwargs: ClipboardTarget("x11", "xclip", "/usr/bin/xclip"),
        spawn=spawn,
        submit_probe=loop.submit,
        schedule_ms=loop.schedule,
        cancel=loop.cancel,
        watch_child=loop.watch,
        signal_helper=loop.signal,
    )

    assert writer.copy(CODE) is None
    assert "xclip cannot mark" in caplog.text
    assert CODE not in caplog.text


def test_send_signal_uses_a_pidfd_for_an_inert_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ticket = ClipboardTicket("wl-copy", process, pidfd=os.pidfd_open(process.pid))
    try:
        send_signal(ticket, signal.SIGTERM)
        assert process.wait(timeout=10) == -signal.SIGTERM
        # An exited ticket is never signalled again.
        ticket.returncode = process.returncode
        send_signal(ticket, signal.SIGKILL)
    finally:
        os.close(ticket.pidfd)
        if process.poll() is None:
            process.kill()
            process.wait()
