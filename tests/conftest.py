"""Fail-closed isolation from the user's desktop and paired devices."""
from __future__ import annotations

import os
import threading

import dbus
import pytest

from blueferry import bus as bus_module

# conftest is loaded before pytest imports test modules. Poison live bus
# addresses here—not merely in a fixture—so GTK/Gio collection-time probes
# cannot reach the desktop either.
PRIVATE_BUS_ADDRESS_ENV = "BLUEFERRY_TEST_DBUS_ADDRESS"
_active_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
_expected_test_bus = os.environ.get(PRIVATE_BUS_ADDRESS_ENV)
_running_private_suite = bool(
    _active_bus and _active_bus == _expected_test_bus
)
_unreachable_bus = "unix:path=/tmp/blueferry-tests-no-live-bus"
if not _running_private_suite:
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = _unreachable_bus
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = _unreachable_bus

# Qt tests must not adopt the operator's desktop. A platform theme plugin is
# the dangerous one: QT_QPA_PLATFORMTHEME=gtk3 pulls GTK3 into a process where
# the GTK4 client tests have already initialized gi, and the two GLib
# thread-default context stacks deadlock the suite. Force a headless platform
# and no theme plugin before any test module can import PySide6.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
# GTK and Qt run in separate processes in production, but these tests load
# both toolkits. With Qt 6.11, GTK initializing GLib before Qt can leave Qt's
# worker dispatcher cleaning up after GLib's thread-local context stack has
# been destroyed. Use Qt's native dispatcher here; tests that need GLib
# dispatch explicitly iterate its context themselves.
os.environ["QT_NO_GLIB"] = "1"
# Package builds can reuse Qt's per-user compiled QML cache even though they
# are testing a newly extracted source tree at the same path. Always compile
# the QML under test from its current source.
os.environ["QML_DISABLE_DISK_CACHE"] = "1"
os.environ.pop("QT_QPA_PLATFORMTHEME", None)
os.environ.pop("QT_STYLE_OVERRIDE", None)


def _forbid_live_bus(kind: str):
    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            f"test attempted to open the real {kind} D-Bus; inject a fake "
            "connection or use the private_dbus marker"
        )

    return forbidden


@pytest.fixture(autouse=True)
def isolate_dbus(monkeypatch, request):
    """Make accidental BlueZ, OBEX, daemon, and notification access fatal.

    The integration test is allowed only when its caller records the address
    created by dbus-run-session. Merely having a desktop session bus is never
    enough to opt a test into external I/O.
    """
    if request.node.get_closest_marker("private_dbus") is not None:
        active = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        expected = os.environ.get(PRIVATE_BUS_ADDRESS_ENV)
        if not active or active != expected:
            pytest.skip("requires an explicitly isolated dbus-run-session")
        return

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", _unreachable_bus)
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", _unreachable_bus)
    monkeypatch.setattr(bus_module, "_thread_state", threading.local())
    monkeypatch.setattr(dbus, "SessionBus", _forbid_live_bus("session"))
    monkeypatch.setattr(dbus, "SystemBus", _forbid_live_bus("system"))


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every BlueFerry configuration and state path at ``tmp_path``."""
    from blueferry import config

    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "LOCAL_ENV_PATH", config_dir / "local.env")
    monkeypatch.setattr(config, "SETTINGS_JSON", config_dir / "settings.json")
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "EVENTS_DB", state_dir / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", state_dir / "contacts.sqlite")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    return tmp_path


@pytest.fixture
def make_daemon(isolated_state, monkeypatch):
    """Build real daemons against isolated state.

    Construction performs no D-Bus or Bluetooth I/O, so tests exercise the
    daemon's actual wiring instead of hand-assembling its private fields.
    Replace a hardware-facing collaborator on the instance when a test needs
    to observe it.
    """
    from blueferry import daemon as daemon_mod
    from blueferry.obex import worker as worker_mod

    # The worker thread would otherwise open its own bus connection.
    monkeypatch.setattr(worker_mod, "initialize_obex_worker_bus", lambda: None)
    monkeypatch.setattr(worker_mod, "close_obex_worker_bus", lambda: None)
    monkeypatch.setattr(daemon_mod, "installed_release", lambda: "0.6.0-6")
    monkeypatch.setattr(daemon_mod, "installed_build_sha", lambda: None)
    built = []

    def make():
        instance = daemon_mod.Daemon()
        # Tests may swap these on the instance; always release the originals.
        built.append((instance.obex_worker, instance.storage))
        return instance

    yield make
    for worker, storage in built:
        worker.shutdown()
        storage.close()
