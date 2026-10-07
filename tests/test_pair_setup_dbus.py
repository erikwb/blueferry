"""Exercise bond inspection against a real, unresponsive bus peer."""
from __future__ import annotations

import time

import dbus
import dbus.mainloop
import pytest

from blueferry import pair_setup

pytestmark = pytest.mark.private_dbus


def test_bond_check_timeout_bounds_a_wedged_bluetoothd(monkeypatch):
    # Owns org.bluez but is never dispatched, like a wedged bluetoothd.
    wedged = dbus.SystemBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
    client = dbus.SystemBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
    monkeypatch.setattr(pair_setup, "get_system_bus", lambda: client)
    try:
        assert wedged.request_name(
            "org.bluez", dbus.bus.NAME_FLAG_DO_NOT_QUEUE,
        ) == dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER
        started = time.monotonic()
        status = pair_setup.bond_status("AA:BB:CC:DD:EE:FF", "hci0", timeout=0.5)
        elapsed = time.monotonic() - started
    finally:
        client.close()
        wedged.release_name("org.bluez")
        wedged.close()

    assert status is None
    # dbus-python's own Introspect call would add its default 25 s here.
    assert elapsed < 5
