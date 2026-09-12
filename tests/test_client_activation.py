"""Notification routing stays independent of a daemon/client restart."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import dbus
import pytest

from blueferry import client_activation as activation


@pytest.fixture
def preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(activation.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(activation.os, "access", lambda *_args: True)
    return tmp_path


@pytest.mark.parametrize("desktop,key", [
    ("GNOME", "gtk"), ("ubuntu:GNOME", "gtk"), ("KDE", "qt"),
    ("Hyprland", "quickshell"), ("", "gtk"),
])
def test_first_launch_uses_desktop(preferences, desktop, key):
    assert activation.select_client([], environment={"XDG_CURRENT_DESKTOP": desktop}).key == key


def test_running_client_wins_over_last_used_and_desktop(preferences):
    activation.record_client_use("qt")
    client = activation.select_client(
        [activation.CLIENTS[2].bus_name], environment={"XDG_CURRENT_DESKTOP": "GNOME"},
    )
    assert client.key == "quickshell"


def test_legacy_gtk_counts_as_running_after_upgrade(preferences):
    activation.record_client_use("qt")
    client = activation.select_client(
        [activation.GTK_CLIENT.desktop_id], environment={"XDG_CURRENT_DESKTOP": "KDE"},
    )
    assert client == activation.GTK_CLIENT


def test_gtk_finishing_startup_uses_new_endpoint_after_activation(preferences):
    calls = []

    def get_object(name, _path, **_kwargs):
        if name == activation.GTK_CLIENT.desktop_id:
            return SimpleNamespace(Activate=lambda data, **_kw: calls.append(("activate", dict(data))))
        assert name == activation.GTK_CLIENT.bus_name  # no legacy daemon call
        return SimpleNamespace(OpenMessage=lambda *args, **_kw: calls.append(("open", args)))

    bus = SimpleNamespace(get_object=get_object, name_has_owner=lambda _name: True)
    assert activation._open_legacy_gtk(bus, "message", "single-use-token")
    assert calls == [
        ("activate", {"activation-token": "single-use-token", "desktop-startup-id": "single-use-token"}),
        ("open", ("message", "")),
    ]


@pytest.mark.parametrize("raises", [False, True])
def test_legacy_gtk_exit_during_forwarding_allows_fresh_start(preferences, monkeypatch, raises):
    def forward(*_args):
        if raises:
            raise dbus.DBusException("old window exited")
        return False

    launched = []
    monkeypatch.setattr(activation, "_open_legacy_gtk", forward)
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kw: SimpleNamespace(
        list_names=lambda: [activation.GTK_CLIENT.desktop_id],
        name_has_owner=lambda _name: False, close=lambda: None,
    ))
    monkeypatch.setattr(activation.subprocess, "Popen", lambda argv, **_kw: launched.append(argv))
    assert activation.forward_to_legacy_gtk("handle", "") is None
    assert activation.open_message("handle", "")
    assert launched == [[activation.GTK_CLIENT.executable, "--message=handle"]]


def test_most_recent_running_client_wins(preferences, monkeypatch):
    monkeypatch.setattr(activation.time, "time_ns", lambda: 100)
    activation.record_client_use("gtk")
    monkeypatch.setattr(activation.time, "time_ns", lambda: 200)
    activation.record_client_use("qt")
    assert activation.select_client([c.bus_name for c in activation.CLIENTS]).key == "qt"
    assert activation.select_client([]).key == "qt"


def test_removed_preference_and_corrupt_file_fall_back(preferences, monkeypatch):
    activation.record_client_use("qt")
    monkeypatch.setattr(activation.os, "access", lambda path, _mode: path.endswith("gtk"))
    assert activation.select_client([]).key == "gtk"
    (preferences / "clients" / "qt").write_text("invalid")
    monkeypatch.setattr(activation.os, "access", lambda *_args: True)
    assert activation.select_client([], environment={}).key == "gtk"
    monkeypatch.setattr(activation.os, "access", lambda *_args: False)
    assert activation.select_client([]) is None


def test_recency_files_are_private_and_leave_daemon_settings_alone(preferences):
    settings = preferences / "settings.json"
    settings.write_text('{"storage_policy":"keep"}')
    activation.record_client_use("qt")
    activation.record_client_use("gtk")
    assert settings.read_text() == '{"storage_policy":"keep"}'
    assert (preferences / "clients" / "qt").stat().st_mode & 0o777 == 0o600
    assert (preferences / "clients").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("token", ["", "wayland-token"])
def test_closed_client_gets_handle_and_only_current_token(preferences, monkeypatch, token):
    launched = []
    closed = []
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "stale")
    monkeypatch.setenv("DESKTOP_STARTUP_ID", "stale")
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kwargs: SimpleNamespace(
        list_names=lambda: [], close=lambda: closed.append(True),
    ))
    monkeypatch.setattr(activation.subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)))
    handle = "opaque handle; $(do not execute)"
    assert activation.open_message(handle, token)
    argv, kwargs = launched[0]
    assert argv == ["/usr/bin/blueferry-qt", f"--message={handle}"]
    assert kwargs["env"].get("XDG_ACTIVATION_TOKEN", "") == token
    assert kwargs["env"].get("DESKTOP_STARTUP_ID", "") == token
    assert "shell" not in kwargs
    assert closed == [True]


@pytest.mark.parametrize("still_running", [False, True])
def test_exit_race_relaunches_but_timeout_does_not_duplicate(preferences, monkeypatch, still_running):
    launched = []

    def failed(*_args, **_kwargs):
        raise dbus.DBusException("lost client")

    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kwargs: SimpleNamespace(
        list_names=lambda: [activation.CLIENTS[0].bus_name], close=lambda: None,
        get_object=lambda *_args, **_kwargs: SimpleNamespace(OpenMessage=failed),
        name_has_owner=lambda _name: still_running,
    ))
    monkeypatch.setattr(activation.subprocess, "Popen", lambda *_args, **_kw: launched.append(True))
    assert activation.open_message("handle", "") is not still_running
    assert bool(launched) is not still_running


@pytest.mark.private_dbus
@pytest.mark.parametrize("key", ["gtk", "qt", "quickshell", "systemd"])
def test_real_client_dbus_activation(tmp_path, monkeypatch, key):
    """Exercise both actual event-loop adapters on an isolated session bus."""
    monkeypatch.setattr(activation.config, "CONFIG_DIR", tmp_path)
    environment = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path))
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), key], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )

    def read_line():
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "client activation timed out"
        line = process.stdout.readline()
        assert line, process.stderr.read()
        return line.decode().strip()

    try:
        if key == "qt":
            assert json.loads(read_line()) == ["during-load", "early-token"]
        assert read_line() == "ready"
        if key == "systemd":
            monkeypatch.setattr(activation.os, "access", lambda *_args: True)
            monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
            assert activation.open_message("-$HOME literal", "focus-token")
            unit, mode, properties, aux = json.loads(read_line())
            properties = dict(properties)
            assert unit.startswith("app-blueferry-gtk-") and unit.endswith(".service")
            assert mode == "fail" and aux == []
            assert properties["Type"] == "exec"
            assert properties["ExecStartEx"] == [[
                "/usr/bin/blueferry-gtk", ["/usr/bin/blueferry-gtk", "--message=-$HOME literal"],
                ["no-env-expand"],
            ]]
            assert "XDG_ACTIVATION_TOKEN=focus-token" in properties["Environment"]
            assert "DESKTOP_STARTUP_ID=focus-token" in properties["Environment"]
        else:
            assert activation.open_message("opaque message", "focus-token")
            assert json.loads(read_line()) == ["opaque message", "focus-token"]
        if key == "qt":
            # A second Qt invocation must forward to the first event loop.
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "qt-forward"],
                env=environment, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(read_line()) == ["second", "second-token"]
    finally:
        process.terminate()
        process.communicate(timeout=5)


def _serve(key):
    def received(handle, token):
        print(json.dumps([handle, token]), flush=True)

    if key.startswith("qt"):
        from PySide6.QtCore import QCoreApplication

        from blueferry.qt.activation import ClientActivation

        app = QCoreApplication([])
        service = ClientActivation(app)
        if key == "qt-forward":
            assert not service.primary
            assert service.forward("second", "second-token")
            return
        assert service.primary
        service.OpenMessage("during-load", "early-token")
        service.requested.connect(received)
        service.ready()
        print("ready", flush=True)
        app.exec()
    else:
        from gi.repository import GLib

        from blueferry.glib_client_activation import ClientActivation

        if key == "systemd":
            import dbus.service

            from blueferry.bus import get_session_bus

            class Manager(dbus.service.Object):
                @dbus.service.method(
                    "org.freedesktop.systemd1.Manager", in_signature="ssa(sv)a(sa(sv))",
                    out_signature="o",
                )
                def StartTransientUnit(self, unit, mode, properties, aux):
                    print(json.dumps([unit, mode, properties, aux]), flush=True)
                    return "/org/freedesktop/systemd1/job/1"

            name = dbus.service.BusName("org.freedesktop.systemd1", get_session_bus())
            service = Manager(name, "/org/freedesktop/systemd1")
        else:
            service = ClientActivation(key, received)
        print("ready", flush=True)
        GLib.MainLoop().run()


if __name__ == "__main__":
    _serve(sys.argv[1])
