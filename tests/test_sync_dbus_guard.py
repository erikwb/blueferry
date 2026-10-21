"""The conftest guard reports blocking D-Bus calls made by daemon code."""
from __future__ import annotations

import os
import threading

import dbus
import dbus.service
import pytest

from blueferry import daemon

pytestmark = pytest.mark.private_dbus

_DAEMON_FILE = os.path.abspath(daemon.__file__)
_SOURCE = """
def probe_bus_id(bus):
    return bus.get_object(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False,
    ).GetId(dbus_interface="org.freedesktop.DBus")
"""


def _daemon_function():
    # Compiled under daemon.py's path, so frames look like daemon code.
    namespace: dict = {}
    exec(compile(_SOURCE, _DAEMON_FILE, "exec"), namespace)
    return namespace["probe_bus_id"]


@pytest.fixture
def bus():
    connection = dbus.SessionBus(private=True)
    yield connection
    connection.close()


def test_blocking_call_from_daemon_code_is_reported(bus, sync_dbus_guard) -> None:
    assert _daemon_function()(bus)
    assert sync_dbus_guard == ["blueferry.daemon:probe_bus_id -> GetId"]
    sync_dbus_guard.clear()  # this test made the call on purpose


def test_tests_and_worker_threads_may_block(bus, sync_dbus_guard) -> None:
    assert bus.get_object(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False,
    ).GetId(dbus_interface="org.freedesktop.DBus")
    worker = threading.Thread(target=_daemon_function(), args=(bus,))
    worker.start()
    worker.join()
    assert sync_dbus_guard == []


_FINALIZER_SOURCE = """
class Holder:
    def __init__(self, bus):
        self.bus = bus

    def __del__(self):
        self.bus.get_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False,
        ).GetId(dbus_interface="org.freedesktop.DBus")


def allocate(bus):
    holder = Holder(bus)
    del holder  # the finalizer runs inside this daemon function
    return True
"""


def test_finalizers_and_bus_name_release_are_not_attributed(bus, sync_dbus_guard) -> None:
    namespace: dict = {}
    exec(compile(_FINALIZER_SOURCE, _DAEMON_FILE, "exec"), namespace)
    assert namespace["allocate"](bus)

    def claim_and_drop(connection):
        name = dbus.service.BusName("io.weirdware.BlueFerry.GuardTest", connection)
        del name  # BusName.__del__ -> ReleaseName, a blocking call

    exec(compile("def claim(bus, run):\n    run(bus)\n", _DAEMON_FILE, "exec"), namespace)
    namespace["claim"](bus, claim_and_drop)
    assert sync_dbus_guard == []


_HELPER_SOURCE = """
import dbus.service


def owner_of(bus, name):
    return bus.get_name_owner(name)


def claim(bus, name):
    return dbus.service.BusName(name, bus)


def proxy_for(bus, name):
    return bus.get_object(name, "/org/freedesktop/DBus", introspect=False)


def release(bus, name):
    return bus.release_name(name)
"""


def test_bus_helpers_called_from_daemon_code_are_reported(bus, sync_dbus_guard) -> None:
    namespace: dict = {}
    exec(compile(_HELPER_SOURCE, _DAEMON_FILE, "exec"), namespace)
    name = "io.weirdware.BlueFerry.GuardHelperTest"
    bus_name = namespace["claim"](bus, name)  # RequestName via BusName: implicit
    assert namespace["proxy_for"](bus, name) is not None  # GetNameOwner via get_object: implicit
    assert sync_dbus_guard == []

    assert namespace["owner_of"](bus, name)
    namespace["release"](bus, name)
    assert sync_dbus_guard == [
        "blueferry.daemon:owner_of -> GetNameOwner",
        "blueferry.daemon:release -> ReleaseName",
    ]
    sync_dbus_guard.clear()  # this test made the calls on purpose
    del bus_name
