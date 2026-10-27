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


def _installed_only(monkeypatch, predicate):
    monkeypatch.setattr(activation, "client_program", lambda client: (
        [f"/usr/bin/{client.program_name}"] if predicate(client) else None
    ))


@pytest.fixture
def preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(activation.config, "CONFIG_DIR", tmp_path)
    _installed_only(monkeypatch, lambda _client: True)
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
    assert launched == [["/usr/bin/blueferry-gtk", "--message=handle"]]


def test_most_recent_running_client_wins(preferences, monkeypatch):
    monkeypatch.setattr(activation.time, "time_ns", lambda: 100)
    activation.record_client_use("gtk")
    monkeypatch.setattr(activation.time, "time_ns", lambda: 200)
    activation.record_client_use("qt")
    assert activation.select_client([c.bus_name for c in activation.CLIENTS]).key == "qt"
    assert activation.select_client([]).key == "qt"


def test_removed_preference_and_corrupt_file_fall_back(preferences, monkeypatch):
    activation.record_client_use("qt")
    _installed_only(monkeypatch, lambda client: client.key == "gtk")
    assert activation.select_client([]).key == "gtk"
    (preferences / "clients" / "qt").write_text("invalid")
    _installed_only(monkeypatch, lambda _client: True)
    assert activation.select_client([], environment={}).key == "gtk"
    _installed_only(monkeypatch, lambda _client: False)
    assert activation.select_client([]) is None


def test_ranked_clients_puts_running_first_then_installed_by_recency(preferences, monkeypatch):
    for stamp, key in ((100, "quickshell"), (200, "gtk")):
        monkeypatch.setattr(activation.time, "time_ns", lambda stamp=stamp: stamp)
        activation.record_client_use(key)
    qt = next(client for client in activation.CLIENTS if client.key == "qt")
    ranked = activation.ranked_clients([qt.bus_name], environment={"XDG_CURRENT_DESKTOP": "KDE"})
    assert [client.key for client in ranked] == ["qt", "gtk", "quickshell"]
    # A client that is not installed drops out; select_client takes the head.
    ranked = activation.ranked_clients(
        [], environment={}, installed=lambda client: client.key != "gtk",
    )
    assert [client.key for client in ranked] == ["quickshell", "qt"]
    assert activation.select_client([], environment={}).key == "gtk"


@pytest.fixture
def no_clients(monkeypatch, tmp_path):
    """No client anywhere: an empty PATH, /usr/bin and venv, no modules."""
    python = sys.executable
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setattr(activation, "_SYSTEM_BIN", tmp_path / "usr-bin")
    monkeypatch.setattr(activation.sys, "executable", str(venv / "python"))
    toolkits: set[str] = set()
    monkeypatch.setattr(
        activation.importlib.util, "find_spec",
        lambda name: object() if name in toolkits else None,
    )
    return SimpleNamespace(root=tmp_path, toolkits=toolkits, python=python)


def _launcher(directory: Path, key: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"blueferry-{key}"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_client_program_finds_a_launcher_on_path(no_clients, monkeypatch):
    no_clients.toolkits.add("gi")
    local_bin = no_clients.root / "home" / ".local" / "bin"
    gtk = _launcher(local_bin, "gtk")
    monkeypatch.setenv("PATH", str(local_bin))
    assert activation.client_program(activation.GTK_CLIENT) == [str(gtk)]
    assert activation.client_program(activation.CLIENTS[2]) is None


def test_client_program_finds_a_venv_launcher_off_path(no_clients):
    # A D-Bus activated daemon on OpenRC, or systemd's PATH, may not include
    # the virtual environment the daemon and its helper run from.
    no_clients.toolkits.add("PySide6")
    qt = _launcher(no_clients.root / "venv" / "bin", "qt")
    assert activation.client_program(activation.CLIENTS[1]) == [str(qt)]


@pytest.mark.parametrize("location", ["path", "system"])
def test_external_launcher_does_not_need_the_helpers_toolkit(no_clients, monkeypatch, location):
    directory = no_clients.root / ("other-venv/bin" if location == "path" else "usr-bin")
    qt = _launcher(directory, "qt")
    if location == "path":
        monkeypatch.setenv("PATH", str(directory))
    assert not no_clients.toolkits
    assert activation.client_program(activation.CLIENTS[1]) == [str(qt)]


@pytest.mark.parametrize("location", ["path", "system"])
def test_unusable_local_launcher_does_not_hide_an_external_one(no_clients, monkeypatch, location):
    local = no_clients.root / "venv" / "bin"
    _launcher(local, "qt")
    external = _launcher(no_clients.root / ("another-bin" if location == "path" else "usr-bin"), "qt")
    monkeypatch.setenv("PATH", os.pathsep.join(
        (str(local), str(external.parent)) if location == "path" else (str(local),)
    ))
    assert activation.client_program(activation.CLIENTS[1]) == [str(external)]


def test_launcher_without_its_toolkit_does_not_count(no_clients):
    # A venv gets both gui-scripts even when PySide6 or gi are missing.
    no_clients.toolkits.add("gi")
    venv = no_clients.root / "venv" / "bin"
    _launcher(venv, "qt")
    gtk = _launcher(venv, "gtk")
    assert activation.client_program(activation.CLIENTS[1]) is None
    assert activation.client_program(activation.GTK_CLIENT) == [str(gtk)]
    assert activation.select_client([], environment={"XDG_CURRENT_DESKTOP": "KDE"}).key == "gtk"


def test_client_program_falls_back_to_the_installed_module(no_clients):
    no_clients.toolkits.add("PySide6")
    python = str(no_clients.root / "venv" / "bin" / "python")
    assert activation.client_program(activation.CLIENTS[1]) == [python, "-m", "blueferry.qt.app"]
    assert activation.client_program(activation.GTK_CLIENT) is None  # no gi here
    assert activation.select_client([], environment={}).key == "qt"


@pytest.mark.parametrize("systemd", [False, True])
def test_open_message_starts_a_venv_client_by_absolute_path(no_clients, monkeypatch, systemd):
    monkeypatch.setattr(activation.config, "CONFIG_DIR", no_clients.root / "config")
    no_clients.toolkits.add("gi")
    gtk = _launcher(no_clients.root / "venv" / "bin", "gtk")
    started = []
    names = ["org.freedesktop.systemd1"] if systemd else []
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kw: SimpleNamespace(
        list_names=lambda: names, close=lambda: None,
    ))
    monkeypatch.setattr(activation, "start_transient_service",
                        lambda _bus, _prefix, argv, _variables: started.append(("unit", argv)))
    monkeypatch.setattr(activation.subprocess, "Popen",
                        lambda argv, **_kw: started.append(("spawn", argv)))
    assert activation.open_message("handle", "")
    assert started == [("unit" if systemd else "spawn", [str(gtk), "--message=handle"])]


@pytest.mark.parametrize("module", [False, True])
def test_transient_unit_passes_the_module_path_only_for_the_module_fallback(
    no_clients, monkeypatch, module,
):
    monkeypatch.setattr(activation.config, "CONFIG_DIR", no_clients.root / "config")
    monkeypatch.setenv("PYTHONPATH", "/checkout/src")
    monkeypatch.setenv("VIRTUAL_ENV", "/checkout/.venv")
    no_clients.toolkits.add("PySide6")
    if not module:
        _launcher(no_clients.root / "venv" / "bin", "qt")
    units = []
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kw: SimpleNamespace(
        list_names=lambda: ["org.freedesktop.systemd1"], close=lambda: None,
    ))
    monkeypatch.setattr(activation, "start_transient_service",
                        lambda _bus, _prefix, argv, variables: units.append((argv, variables)))
    assert activation.open_message("handle", "")
    argv, variables = units[0]
    assert (argv[1:3] == ["-m", "blueferry.qt.app"]) is module
    environment = dict(value.split("=", 1) for value in variables)
    if module:
        assert "/checkout/src" in environment["PYTHONPATH"].split(os.pathsep)
        assert environment["VIRTUAL_ENV"] == "/checkout/.venv"
    else:
        assert "PYTHONPATH" not in environment and "VIRTUAL_ENV" not in environment


@pytest.mark.parametrize("pythonpath", ["src", "", None])
def test_module_fallback_imports_the_checkout_from_a_transient_units_directory(
    no_clients, monkeypatch, pythonpath,
):
    checkout = no_clients.root / "checkout"
    package = checkout / "src" / "blueferry"
    ui = package / "ui"
    ui.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (ui / "__init__.py").write_text("")
    (ui / "app.py").write_text('print("inert client started")\n')
    monkeypatch.setattr(activation, "__file__", str(package / "client_activation.py"))
    monkeypatch.setattr(activation.sys, "executable", no_clients.python)
    monkeypatch.setattr(activation.config, "CONFIG_DIR", no_clients.root / "config")
    if pythonpath is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(activation, "client_program", lambda client: (
        activation._module_program(client) if client.key == "gtk" else None
    ))
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kw: SimpleNamespace(
        list_names=lambda: ["org.freedesktop.systemd1"], close=lambda: None,
    ))
    units = []
    monkeypatch.setattr(activation, "start_transient_service",
                        lambda _bus, _prefix, argv, variables: units.append((argv, variables)))
    assert activation.open_message("message", "")
    argv, variables = units[0]
    # -S keeps this inert fixture independent of any installed BlueFerry.
    result = subprocess.run(
        [argv[0], "-S", *argv[1:]], cwd=no_clients.root,
        env=dict(value.split("=", 1) for value in variables),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "inert client started"


def test_open_message_without_any_client_does_nothing(no_clients, monkeypatch):
    monkeypatch.setattr(activation.config, "CONFIG_DIR", no_clients.root / "config")
    monkeypatch.setattr(activation.dbus, "SessionBus", lambda **_kw: SimpleNamespace(
        list_names=lambda: [], close=lambda: None,
    ))
    assert not activation.open_message("handle", "")


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
    if key == "qt":
        # Debian package builds do not require the optional Qt runtime.
        pytest.importorskip("PySide6")
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
            _installed_only(monkeypatch, lambda _client: True)
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


@pytest.mark.private_dbus
@pytest.mark.parametrize("key", ["gtk", "qt", "quickshell"])
def test_real_client_dial_activation_only_requests_confirmation(tmp_path, key):
    """``Dial`` is open to every session process: it validates and only asks."""
    if key == "qt":
        pytest.importorskip("PySide6")
    environment = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path))
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), f"{key}-dial"], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )

    def read_line():
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "client activation timed out"
        line = process.stdout.readline()
        assert line, process.stderr.read()
        return line.decode().strip()

    client = next(client for client in activation.CLIENTS if client.key == key)
    bus = dbus.SessionBus(private=True)
    try:
        if key == "qt":
            # Requests that arrive while QML loads wait for the window.
            assert json.loads(read_line()) == ["dial", "tel:early", "early-token"]
        assert read_line() == "ready"
        service = bus.get_object(client.bus_name, activation.ACTIVATION_PATH, introspect=False)

        def dial(link, token=""):
            service.Dial(link, token, dbus_interface=activation.ACTIVATION_INTERFACE, timeout=3)

        if key == "quickshell":
            with pytest.raises(dbus.DBusException, match="cannot place calls"):
                dial("tel:+15550100000")
            return
        oversized = "tel:" + "1" * 1021
        if key == "gtk":
            for link, token in (("", ""), (oversized, ""), ("tel:1", "t" * 4097)):
                with pytest.raises(dbus.DBusException, match="invalid activation request"):
                    dial(link, token)
        else:
            # Qt ignores invalid requests without an error reply.
            dial("")
            dial(oversized)
        # The raw link is passed on; the window parses it again and asks.
        dial("tel:+1 555 010 0000;ext=1", "focus-token")
        assert json.loads(read_line()) == ["dial", "tel:+1 555 010 0000;ext=1", "focus-token"]
        if key == "qt":
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "qt-dial-forward"],
                env=environment, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(read_line()) == ["dial", "callto:+15550100001", "second-token"]
    finally:
        bus.close()
        process.terminate()
        process.communicate(timeout=5)


def _serve(key):
    def received(handle, token):
        print(json.dumps([handle, token]), flush=True)

    def dial_received(link, token):
        print(json.dumps(["dial", link, token]), flush=True)

    if key.startswith("qt"):
        from PySide6.QtCore import QCoreApplication

        from blueferry.qt.activation import ClientActivation

        app = QCoreApplication([])
        service = ClientActivation(app)
        if key == "qt-dial-forward":
            assert not service.primary
            assert service.forward_dial("callto:+15550100001", "second-token")
            return
        if key == "qt-dial":
            assert service.primary
            service.Dial("tel:early", "early-token")
            service.dialRequested.connect(dial_received)
            service.requested.connect(received)
            service.ready()
            print("ready", flush=True)
            app.exec()
            return
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
        elif key == "gtk-dial":
            service = ClientActivation("gtk", received, dial_received)
        elif key == "quickshell-dial":
            service = ClientActivation("quickshell", received)
        else:
            service = ClientActivation(key, received)
        print("ready", flush=True)
        GLib.MainLoop().run()


if __name__ == "__main__":
    _serve(sys.argv[1])
