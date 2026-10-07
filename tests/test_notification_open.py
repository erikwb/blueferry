"""Launching a notification click target never touches a real desktop."""
from __future__ import annotations

import subprocess
import sys
from typing import ClassVar

import dbus
import pytest

from blueferry import notification_open
from blueferry.notification_open import (
    helper_argv,
    launch_target,
    open_target,
    request_open_target,
)
from blueferry.notification_open_map import DESKTOP_TARGET, URL_TARGET, OpenTarget

URL = OpenTarget(URL_TARGET, "https://web.whatsapp.com/?a=1&b=$HOME")
DESKTOP = OpenTarget(DESKTOP_TARGET, "org.mozilla.Thunderbird.desktop")


class _Spawn:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return object()


def test_helper_argv_is_fixed_module_plus_one_target_token() -> None:
    assert helper_argv(URL) == [
        sys.executable, "-m", "blueferry.notification_open", f"--url={URL.value}",
    ]
    assert helper_argv(DESKTOP, direct=True) == [
        sys.executable, "-m", "blueferry.notification_open", "--direct",
        "--desktop-id=org.mozilla.Thunderbird.desktop",
    ]


def test_daemon_spawns_helper_with_only_the_single_use_token(monkeypatch) -> None:
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "stale-token")
    monkeypatch.setenv("DESKTOP_STARTUP_ID", "stale-token")
    spawn = _Spawn()

    assert request_open_target(URL, "fresh-token", spawn=spawn) is True

    [(argv, kwargs)] = spawn.calls
    assert argv == helper_argv(URL)
    assert kwargs["env"]["XDG_ACTIVATION_TOKEN"] == "fresh-token"
    assert kwargs["env"]["DESKTOP_STARTUP_ID"] == "fresh-token"
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs

    spawn.calls.clear()
    assert request_open_target(DESKTOP, "", spawn=spawn) is True
    [(_argv, kwargs)] = spawn.calls
    assert "XDG_ACTIVATION_TOKEN" not in kwargs["env"]


@pytest.mark.parametrize("forged", [
    OpenTarget(URL_TARGET, "file:///etc/passwd"),
    OpenTarget(URL_TARGET, "javascript:alert(1)"),
    OpenTarget(DESKTOP_TARGET, "https://example.com"),
    OpenTarget(URL_TARGET, "org.mozilla.Thunderbird.desktop"),
    OpenTarget(DESKTOP_TARGET, "../evil.desktop"),
    OpenTarget(DESKTOP_TARGET, "thunderbird.desktop; id"),
])
def test_every_launch_boundary_rejects_unvalidated_targets(forged) -> None:
    spawn = _Spawn()
    assert request_open_target(forged, "token", spawn=spawn) is False
    assert spawn.calls == []
    assert open_target(forged, "token", bus_factory=pytest.fail, launcher=pytest.fail) is False
    assert launch_target(forged, "token", gio=object()) is False


def test_oversized_activation_token_is_refused() -> None:
    spawn = _Spawn()
    assert request_open_target(URL, "x" * 5000, spawn=spawn) is False
    assert spawn.calls == []


def test_spawn_failure_is_reported_not_raised() -> None:
    assert request_open_target(URL, "", spawn=_Spawn(OSError("missing"))) is False


class _Manager:
    def __init__(self, failures=()) -> None:
        self.calls = []
        self.failures = list(failures)

    def StartTransientUnit(self, name, mode, properties, aux, **kwargs):
        self.calls.append((str(name), str(mode), [tuple(item) for item in properties], kwargs))
        if self.failures:
            raise self.failures.pop(0)


class _Bus:
    def __init__(self, names, manager=None) -> None:
        self.names = names
        self.manager = manager or _Manager()
        self.closed = False

    def list_names(self):
        return self.names

    def get_object(self, name, path, **_kwargs):
        assert (name, path) == ("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
        return self.manager

    def close(self) -> None:
        self.closed = True


def _properties(call) -> dict:
    return dict(call[2])


def test_systemd_session_leaves_the_backend_sandbox_via_a_transient_unit(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_DATA_DIRS", "/sandboxed/share")
    monkeypatch.setenv("PATH", "/sandboxed/bin")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    bus = _Bus(["org.freedesktop.systemd1"])

    assert open_target(URL, "click-token", bus_factory=lambda: bus, launcher=pytest.fail)

    assert bus.closed is True
    [call] = bus.manager.calls
    name, mode, _items, kwargs = call
    assert name.startswith("app-blueferry-open-") and name.endswith(".service")
    assert mode == "fail"
    assert kwargs["timeout"] == 3
    properties = _properties(call)
    assert properties["ExitType"] == "cgroup"
    assert properties["Type"] == "exec"
    [(executable, argv, flags)] = [tuple(item) for item in properties["ExecStartEx"]]
    assert executable == sys.executable
    assert list(argv) == helper_argv(URL, direct=True)
    # No systemd $VARIABLE expansion of the '$HOME' inside the URL.
    assert list(flags) == ["no-env-expand"]
    environment = list(properties["Environment"])
    assert "WAYLAND_DISPLAY=wayland-0" in environment
    # PATH and XDG search paths stay the user manager's defaults.
    assert not any(value.startswith(("XDG_DATA_DIRS=", "PATH=")) for value in environment)
    assert "XDG_ACTIVATION_TOKEN=click-token" in environment
    assert not any(value.startswith("UNRELATED_SECRET=") for value in environment)


def test_older_systemd_without_exit_type_keeps_the_launched_app_alive() -> None:
    unknown = dbus.DBusException(
        "Cannot set property ExitType, or unknown property.",
        name="org.freedesktop.DBus.Error.PropertyReadOnly",
    )
    bus = _Bus(["org.freedesktop.systemd1"], _Manager([unknown]))

    assert open_target(DESKTOP, "", bus_factory=lambda: bus, launcher=pytest.fail)

    first, second = bus.manager.calls
    assert "ExitType" in _properties(first)
    assert "ExitType" not in _properties(second)
    assert _properties(second)["KillMode"] == "process"


def test_a_systemd_timeout_is_not_retried_as_a_second_launch() -> None:
    timeout = dbus.DBusException("no reply", name="org.freedesktop.DBus.Error.NoReply")
    bus = _Bus(["org.freedesktop.systemd1"], _Manager([timeout]))

    with pytest.raises(dbus.DBusException):
        open_target(URL, "", bus_factory=lambda: bus, launcher=pytest.fail)

    assert len(bus.manager.calls) == 1
    assert bus.closed is True


def test_without_systemd_the_helper_launches_directly() -> None:
    bus = _Bus([":1.7", "org.freedesktop.Notifications"])
    launched = []

    assert open_target(
        DESKTOP, "tok", bus_factory=lambda: bus,
        launcher=lambda target, token: launched.append((target, token)) or True,
    )

    assert launched == [(DESKTOP, "tok")]
    assert bus.manager.calls == []


def test_unreachable_session_bus_falls_back_to_a_direct_launch() -> None:
    def no_bus():
        raise dbus.DBusException("no bus", name="org.freedesktop.DBus.Error.NoServer")

    launched = []
    assert open_target(
        URL, "tok", bus_factory=no_bus,
        launcher=lambda target, token: launched.append((target, token)) or True,
    )
    assert launched == [(URL, "tok")]


class _Context:
    def __init__(self) -> None:
        self.environment: dict[str, str] = {}

    def get_startup_notify_id(self, info, files):
        # Mirrors GAppLaunchContext dispatching to the Python vfunc.
        return self.do_get_startup_notify_id(info, files)

    def do_get_startup_notify_id(self, _info, _files):
        return None

    def setenv(self, name, value) -> None:
        self.environment[name] = value


class _AppInfo:
    launched: ClassVar[list] = []

    @classmethod
    def launch_default_for_uri(cls, uri, context):
        cls.launched.append(("uri", uri, context))
        return True


class _DesktopAppInfo:
    installed: ClassVar[set[str]] = {"org.mozilla.Thunderbird.desktop"}
    launched: ClassVar[list] = []

    def __init__(self, desktop_id) -> None:
        self.desktop_id = desktop_id

    @classmethod
    def new(cls, desktop_id):
        return cls(desktop_id) if desktop_id in cls.installed else None

    def launch(self, files, context):
        # A DBusActivatable entry only receives the token through the
        # context's startup ID, not through environment variables.
        type(self).launched.append(
            (self.desktop_id, list(files), context, context.get_startup_notify_id(self, files)),
        )
        return True


class _Gio:
    AppLaunchContext = _Context
    AppInfo = _AppInfo
    DesktopAppInfo = _DesktopAppInfo


@pytest.fixture(autouse=True)
def reset_gio():
    _AppInfo.launched = []
    _DesktopAppInfo.launched = []


def test_url_uses_the_default_handler_with_the_activation_token() -> None:
    assert launch_target(URL, "tok", gio=_Gio) is True

    [(kind, uri, context)] = _AppInfo.launched
    assert (kind, uri) == ("uri", URL.value)
    assert context.environment == {"XDG_ACTIVATION_TOKEN": "tok", "DESKTOP_STARTUP_ID": "tok"}
    assert _DesktopAppInfo.launched == []


def test_desktop_entry_is_launched_without_files_or_uris() -> None:
    assert launch_target(DESKTOP, "", gio=_Gio) is True

    [(desktop_id, files, context, startup_id)] = _DesktopAppInfo.launched
    assert desktop_id == DESKTOP.value
    assert files == []
    assert context.environment == {}
    assert startup_id is None
    assert _AppInfo.launched == []


def test_dbus_activatable_entries_receive_the_token_as_startup_id() -> None:
    assert launch_target(DESKTOP, "wayland-token", gio=_Gio) is True

    [(_desktop_id, _files, context, startup_id)] = _DesktopAppInfo.launched
    assert startup_id == "wayland-token"
    assert context.environment["XDG_ACTIVATION_TOKEN"] == "wayland-token"


def test_token_context_works_with_the_real_gio_class() -> None:
    gio = pytest.importorskip("gi.repository.Gio")
    # Older GLib (Ubuntu 24.04) does not accept None for the app info here,
    # so pass a real one. It is only created, never launched.
    info = gio.AppInfo.create_from_commandline("true", None, gio.AppInfoCreateFlags.NONE)
    context = notification_open._token_context_class(gio)("real-token")

    assert context.get_startup_notify_id(info, []) == "real-token"
    assert notification_open._token_context_class(gio)("").get_startup_notify_id(info, []) is None


def test_missing_desktop_entry_does_nothing() -> None:
    target = OpenTarget(DESKTOP_TARGET, "com.example.NotInstalled.desktop")
    assert launch_target(target, "", gio=_Gio) is False
    assert _DesktopAppInfo.launched == []
    assert _AppInfo.launched == []


def test_helper_command_line_revalidates_and_dispatches(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        notification_open, "open_target", lambda target, token: calls.append(("open", target, token)) or True,
    )
    monkeypatch.setattr(
        notification_open, "launch_target", lambda target, token: calls.append(("direct", target, token)) or True,
    )
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "tok")

    assert notification_open.main([f"--url={URL.value}"]) == 0
    assert notification_open.main(["--direct", f"--desktop-id={DESKTOP.value}"]) == 0
    assert calls == [("open", URL, "tok"), ("direct", DESKTOP, "tok")]


def test_shell_click_argv_carries_only_the_opaque_id() -> None:
    assert notification_open.click_argv("abc_DEF-123") == [
        sys.executable, "-m", "blueferry.notification_open", "--click=abc_DEF-123",
    ]


def test_click_helper_hands_the_id_and_token_back_to_the_daemon(monkeypatch) -> None:
    calls = []

    class _Backend:
        def open_notification_click(self, click_id, token):
            calls.append((click_id, token))
            return click_id == "known"

    monkeypatch.setattr(notification_open, "open_target", pytest.fail)
    monkeypatch.setattr(notification_open, "launch_target", pytest.fail)
    real = notification_open.forward_click
    monkeypatch.setattr(
        notification_open, "forward_click",
        lambda click_id, token: real(click_id, token, backend_factory=_Backend),
    )
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "tok")

    assert notification_open.main(["--click=known"]) == 0
    assert notification_open.main(["--click=gone"]) == 1
    assert calls == [("known", "tok"), ("gone", "tok")]


def test_click_helper_reports_an_unreachable_daemon(monkeypatch, caplog) -> None:
    def fail(_click_id, _token):
        raise RuntimeError("backend is not running")

    monkeypatch.setattr(notification_open, "forward_click", fail)

    assert notification_open.main(["--click=abc"]) == 1
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("argv", [
    ["--click="],
    ["--click=" + "x" * 65],
    ["--direct", "--click=abc"],
    ["--click=abc", "--url=https://example.com"],
])
def test_click_helper_rejects_malformed_ids(monkeypatch, argv) -> None:
    monkeypatch.setattr(notification_open, "forward_click", pytest.fail)

    with pytest.raises(SystemExit) as raised:
        notification_open.main(argv)
    assert raised.value.code == 2


@pytest.mark.parametrize("argv", [
    ["--url=file:///etc/passwd"],
    ["--url=javascript:alert(1)"],
    ["--url=org.mozilla.Thunderbird.desktop"],
    ["--desktop-id=https://example.com"],
    ["--desktop-id=/usr/bin/id"],
    ["--desktop-id=thunderbird.desktop; id"],
    ["--url=https://example.com", "--desktop-id=a.desktop"],
    [],
])
def test_helper_command_line_rejects_invalid_targets(monkeypatch, argv) -> None:
    monkeypatch.setattr(notification_open, "open_target", pytest.fail)
    monkeypatch.setattr(notification_open, "launch_target", pytest.fail)

    with pytest.raises(SystemExit) as raised:
        notification_open.main(argv)
    assert raised.value.code == 2


def test_helper_failures_are_logged_without_the_target(monkeypatch, caplog) -> None:
    class _GLibError(Exception):
        pass

    def fail(_target, _token):
        raise _GLibError(f"Failed to open {URL.value}")

    monkeypatch.setattr(notification_open, "launch_target", fail)

    assert notification_open.main(["--direct", f"--url={URL.value}"]) == 1
    assert "web.whatsapp.com" not in caplog.text
