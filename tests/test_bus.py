"""D-Bus is acquired at the I/O edge, not as an import side effect."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from blueferry import bus


def test_bus_module_imports_without_reachable_dbus() -> None:
    env = os.environ.copy()
    env.update({
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/does/not/exist/session-bus",
        "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/does/not/exist/system-bus",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    })

    result = subprocess.run(
        [sys.executable, "-c", "import blueferry.bus"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("factory_name", "getter_name"),
    [
        ("SystemBus", "get_system_bus"),
        ("SessionBus", "get_session_bus"),
    ],
)
def test_bus_connections_are_reused_within_their_owning_thread(
    monkeypatch, factory_name, getter_name,
) -> None:
    connection = object()
    creations = 0

    def create(*, private, mainloop):
        nonlocal creations
        assert private is True
        assert mainloop is None
        creations += 1
        return connection

    monkeypatch.setattr(bus, "_thread_state", __import__("threading").local())
    monkeypatch.setattr(bus.dbus, factory_name, create)
    getter = getattr(bus, getter_name)

    assert getter() is connection
    assert getter() is connection
    assert creations == 1


def test_system_bus_connection_is_not_shared_with_worker_thread(monkeypatch) -> None:
    connections = []
    mainloops = []

    def create(*, private, mainloop):
        assert private is True
        mainloops.append(mainloop)
        connection = object()
        connections.append(connection)
        return connection

    monkeypatch.setattr(bus, "_thread_state", threading.local())
    monkeypatch.setattr(bus.dbus, "SystemBus", create)

    main_connection = bus.get_system_bus()
    worker_connections = []
    worker = threading.Thread(
        target=lambda: worker_connections.append(bus.get_system_bus())
    )
    worker.start()
    worker.join()

    assert worker_connections[0] is not main_connection
    assert len(connections) == 2
    assert mainloops == [None, bus.dbus.mainloop.NULL_MAIN_LOOP]
