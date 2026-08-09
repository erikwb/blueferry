"""D-Bus is acquired at the I/O edge, not as an import side effect."""
from __future__ import annotations

import os
import subprocess
import sys
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
    ("cache_name", "factory_name", "getter_name"),
    [
        ("_system_bus", "SystemBus", "get_system_bus"),
        ("_session_bus", "SessionBus", "get_session_bus"),
    ],
)
def test_process_bus_connections_are_reused(
    monkeypatch, cache_name, factory_name, getter_name,
) -> None:
    connection = object()
    creations = 0

    def create():
        nonlocal creations
        creations += 1
        return connection

    monkeypatch.setattr(bus, cache_name, None)
    monkeypatch.setattr(bus.dbus, factory_name, create)
    getter = getattr(bus, getter_name)

    assert getter() is connection
    assert getter() is connection
    assert creations == 1
