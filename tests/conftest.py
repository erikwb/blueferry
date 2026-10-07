"""Fail-closed isolation from the user's desktop and paired devices."""
from __future__ import annotations

import atexit
import functools
import os
import shutil
import sys
import tempfile
import threading

# Before anything imports blueferry.config: its default paths are the
# operator's real configuration, history, and contact cache. Point them at a
# throwaway tree so even a test that undoes its own isolation cannot reach
# private data or load the operator's local.env.
_scratch_home = tempfile.mkdtemp(prefix="blueferry-tests-")
atexit.register(shutil.rmtree, _scratch_home, True)
for _variable, _child in (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_RUNTIME_DIR", "runtime"),
):
    os.makedirs(os.path.join(_scratch_home, _child), mode=0o700)
    os.environ[_variable] = os.path.join(_scratch_home, _child)

import dbus  # noqa: E402
import pytest  # noqa: E402
from gi.repository import GLib  # noqa: E402

# Before anything imports blueferry: record every GLib timer and idle source
# armed from Python so the guard below can fail a test that leaves one behind.
# Supervisors bind GLib.timeout_add_seconds as a default argument at import
# time, so the wrappers must already be in place when those modules load.
# A leaked source fires later on an orphaned object inside whichever test
# happens to iterate the default main context, and on the private test bus
# that can stall an unrelated test on a blocking D-Bus call.
# Raise rather than assert so the ordering check survives ``python -O``.
if any(name == "blueferry" or name.startswith("blueferry.") for name in sys.modules):
    raise RuntimeError("GLib source guard must be installed before blueferry is imported")
_glib_sources_armed: list[tuple[int, str, object]] | None = None


def _record_glib_source(name: str):
    original = getattr(GLib, name)
    assert callable(original), f"GLib.{name} is not callable"

    @functools.wraps(original)
    def armed(*args, **kwargs):
        source_id = original(*args, **kwargs)
        record = _glib_sources_armed
        if record is not None:
            callback = next((arg for arg in args if callable(arg)), None)
            record.append((source_id, name, callback))
        return source_id

    setattr(GLib, name, armed)


for _glib_name in ("timeout_add", "timeout_add_seconds", "idle_add"):
    _record_glib_source(_glib_name)

import dbus.bus  # noqa: E402

# libdbus leaves exit-on-disconnect enabled on dbus-python's private
# connections. Once such a connection is closed, the next dispatch of its
# queued Disconnected message calls _exit(1). A failing test keeps its closed
# connections alive in the traceback, so the next GLib iteration would end
# pytest silently: no traceback, no summary, only "F" and exit status 1.
# Every connection, whether a test or the code under test opens it and
# whatever constructor it uses (SessionBus(True), SystemBus(private=True),
# BusConnection(address)), is created by BusConnection.__new__, so disabling
# the flag there covers them all. Shared connections get it too; they are
# never closed during the run, so that changes nothing for them.
_open_bus_connection = dbus.bus.BusConnection.__new__


def _bus_connection_without_exit_on_disconnect(cls, *args, **kwargs):
    connection = _open_bus_connection(cls, *args, **kwargs)
    connection.set_exit_on_disconnect(False)
    return connection


dbus.bus.BusConnection.__new__ = staticmethod(  # type: ignore[method-assign]
    _bus_connection_without_exit_on_disconnect
)

from blueferry import bus as bus_module  # noqa: E402

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


class GlibSourceGuard:
    """Sources armed through GLib during the current test."""

    def __init__(self) -> None:
        self.armed: list[tuple[int, str, object]] = []

    def live(self) -> list[tuple[int, str, object]]:
        context = GLib.MainContext.default()
        live = []
        for source_id, name, callback in self.armed:
            source = context.find_source_by_id(source_id)
            if source is not None and not source.is_destroyed():
                live.append((source_id, name, callback))
        return live


@pytest.fixture(autouse=True)
def glib_source_guard():
    """Fail a test that leaves a GLib timer or idle source armed.

    Tests inject ``schedule``/``cancel`` fakes into supervisors instead of
    arming real sources. Private D-Bus tests may use real GLib dispatch, but
    everything they arm must have fired or been removed by teardown. This
    fixture is defined first so its teardown runs after every other
    function-scoped fixture has cleaned up.
    """
    global _glib_sources_armed
    guard = GlibSourceGuard()
    _glib_sources_armed = guard.armed
    try:
        yield guard
    finally:
        _glib_sources_armed = None
    leaked = guard.live()
    for source_id, _name, _callback in leaked:
        # Do not let the orphan fire inside a later, unrelated test.
        GLib.source_remove(source_id)
    assert not leaked, (
        "test left GLib sources armed; inject schedule/cancel fakes: "
        + ", ".join(
            f"GLib.{name}({getattr(callback, '__qualname__', None) or repr(callback)})"
            for _id, name, callback in leaked
        )
    )


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
