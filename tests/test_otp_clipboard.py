"""Clipboard helper selection and ownership without touching a real clipboard."""
from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace

import pytest

from blueferry.errors import CommandError
from blueferry.otp_clipboard import (
    ClipboardTarget,
    ClipboardWriter,
    copy_argv,
    find_target,
    supports_sensitive_hint,
)

CODE = "482913"


def _which(*available: str):
    return lambda tool: f"/usr/bin/{tool}" if tool in available else None


def test_wayland_display_selects_wl_copy() -> None:
    target = find_target({"WAYLAND_DISPLAY": "wayland-0"}, which=_which("wl-copy", "xclip"))
    assert target == ClipboardTarget("wayland", "wl-copy", "/usr/bin/wl-copy")


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


def test_runtime_socket_scan_only_accepts_sockets(tmp_path) -> None:
    from blueferry.otp_clipboard import _wayland_sockets

    (tmp_path / "wayland-0").write_text("")
    (tmp_path / "wayland-0.lock").write_text("")
    assert _wayland_sockets(str(tmp_path)) == []
    assert _wayland_sockets(None) == []


def test_x11_falls_back_to_xclip_then_xsel() -> None:
    assert find_target({"DISPLAY": ":0"}, which=_which("xclip", "xsel")).tool == "xclip"
    assert find_target({"DISPLAY": ":0"}, which=_which("xsel")).tool == "xsel"
    assert find_target({"DISPLAY": ":0"}, which=_which()) is None
    assert find_target({}, which=_which("xclip")) is None


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


class _Process:
    def __init__(self, returncode=None) -> None:
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _Timers:
    def __init__(self) -> None:
        self.pending: dict[int, object] = {}
        self.delays: list[int] = []
        self.next_id = 1

    def schedule(self, delay, callback) -> int:
        source = self.next_id
        self.next_id += 1
        self.pending[source] = callback
        self.delays.append(delay)
        return source

    def cancel(self, source) -> None:
        self.pending.pop(source, None)

    def fire_all(self) -> None:
        for source, callback in list(self.pending.items()):
            self.pending.pop(source)
            callback()


def _writer(*, clear_after_s=0, sensitive=True, environ=None):
    spawned = []
    timers = _Timers()

    def spawn(argv, *, stdin_text, env):
        process = _Process()
        spawned.append(SimpleNamespace(argv=argv, stdin=stdin_text, env=env, process=process))
        return process

    writer = ClipboardWriter(
        clear_after_s=clear_after_s,
        environ=environ if environ is not None else {"XDG_RUNTIME_DIR": "/run/user/1000"},
        find=lambda _environ: ClipboardTarget(
            "wayland", "wl-copy", "/usr/bin/wl-copy", (("WAYLAND_DISPLAY", "wayland-0"),)
        ),
        spawn=spawn,
        probe_sensitive=lambda _executable: sensitive,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    return writer, spawned, timers


def test_code_travels_on_stdin_with_the_recovered_display() -> None:
    writer, spawned, timers = _writer()

    assert writer.copy(CODE) == "wl-copy"

    [call] = spawned
    assert call.stdin == CODE
    assert CODE not in " ".join(call.argv)
    assert "--sensitive" in call.argv
    assert call.env["WAYLAND_DISPLAY"] == "wayland-0"
    assert call.env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert timers.pending == {}


def test_auto_clear_stops_a_helper_that_still_owns_the_code() -> None:
    writer, spawned, timers = _writer(clear_after_s=30)
    writer.copy(CODE)

    assert timers.delays == [30]
    timers.fire_all()

    assert spawned[0].process.terminated is True


def test_auto_clear_leaves_a_replaced_clipboard_alone() -> None:
    writer, spawned, timers = _writer(clear_after_s=30)
    writer.copy(CODE)
    # Something else was copied: wl-copy exits on its own.
    spawned[0].process.returncode = 0

    timers.fire_all()

    assert spawned[0].process.terminated is False


def test_new_code_replaces_the_previous_helper_and_its_timer() -> None:
    writer, spawned, timers = _writer(clear_after_s=30)
    writer.copy(CODE)
    writer.copy("135790")

    assert spawned[0].process.terminated is True
    assert len(timers.pending) == 1
    timers.fire_all()
    assert spawned[1].process.terminated is True


def test_release_clears_the_code_on_shutdown() -> None:
    writer, spawned, _timers = _writer()
    writer.copy(CODE)

    writer.release()

    assert spawned[0].process.terminated is True
    assert writer.helper_failed() is True


def test_missing_helper_warns_once_without_the_code(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    writer = ClipboardWriter(
        environ={},
        find=lambda _environ: None,
        schedule=lambda *_args: 0,
        cancel=lambda _source: None,
    )

    assert writer.copy(CODE) is None
    assert writer.copy(CODE) is None

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert CODE not in caplog.text


def test_helper_start_failure_is_logged_without_the_code(caplog) -> None:
    caplog.set_level(logging.DEBUG)

    def spawn(argv, **_kwargs):
        raise CommandError(tuple(argv), f"{argv[0]} could not be started")

    writer = ClipboardWriter(
        environ={},
        find=lambda _environ: ClipboardTarget("x11", "xclip", "/usr/bin/xclip"),
        spawn=spawn,
        schedule=lambda *_args: 0,
        cancel=lambda _source: None,
    )

    assert writer.copy(CODE) is None
    assert "sensitive" in caplog.text
    assert CODE not in caplog.text
