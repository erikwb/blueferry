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
