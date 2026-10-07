"""Exercise the actual dbus-python wire format on an isolated bus."""
from __future__ import annotations

import threading
import time

import dbus
import dbus.lowlevel
import dbus.mainloop
import pytest
from gi.repository import GLib

from blueferry import bluetooth_recovery as mod
from blueferry.settings_store import SettingsStore

pytestmark = pytest.mark.private_dbus


def test_real_dbus_power_cycle_uses_variants_and_clears_its_journal(tmp_path, monkeypatch):
    server = dbus.SystemBus(private=True)
    monitor = dbus.SystemBus(private=True)
    server.request_name("org.bluez", dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    local = threading.local()
    local.bus = monitor
    monkeypatch.setattr(mod, "get_system_bus", lambda: local.bus)
    adapter = mod.BluezRecoveryAdapter(
        "hci99", "11:22:33:44:55:66", settings=SettingsStore(tmp_path / "settings.json"),
    )
    monkeypatch.setattr(adapter, "_adapter_instance", lambda: "simulated-kernel-instance")
    properties = {
        "Address": "AA:BB:CC:DD:EE:FF", "Powered": True, "PowerState": "on",
        "Discovering": False, "Discoverable": False,
    }
    objects = {
        adapter.path: {"org.bluez.Adapter1": properties},
        adapter.device_path: {"org.bluez.Device1": {"Paired": True}},
    }
    writes = []
    errors = []

    def handle(connection, message):
        if message.get_type() != dbus.lowlevel.MESSAGE_TYPE_METHOD_CALL:
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED
        interface = message.get_interface()
        method = message.get_member()
        reply = dbus.lowlevel.MethodReturnMessage(message)
        if interface == "org.freedesktop.DBus.ObjectManager" and method == "GetManagedObjects":
            reply.append(objects, signature="a{oa{sa{sv}}}")
        elif interface == "org.freedesktop.DBus.Properties" and method == "Set":
            # Unlike permissive Python fakes, BlueZ matches the complete
            # signature before dispatching a property setter.
            if message.get_signature() != "ssv":
                connection.send_message(dbus.lowlevel.ErrorMessage(
                    message, "org.freedesktop.DBus.Error.UnknownMethod", "Set requires ssv",
                ))
                return dbus.lowlevel.HANDLER_RESULT_HANDLED
            target, name, value = message.get_args_list()
            if target != "org.bluez.Adapter1" or name != "Powered" or value.variant_level != 1:
                errors.append("invalid property arguments")
            journal = adapter._settings.read().get(mod.BLUETOOTH_RESTORE_KEY)
            if not journal or journal["phase"] != ("on" if value else "off"):
                errors.append("power request preceded its durable journal")
            writes.append(bool(value))
            properties.update(Powered=bool(value), PowerState="on" if value else "off")
            signal = dbus.lowlevel.SignalMessage(
                adapter.path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            )
            signal.append("org.bluez.Adapter1", {"Powered": value}, [], signature="sa{sv}as")
            connection.send_message(signal)
        else:
            errors.append(f"unexpected {interface}.{method}")
            return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED
        connection.send_message(reply)
        return dbus.lowlevel.HANDLER_RESULT_HANDLED

    server.add_message_filter(handle)
    adapter.start_monitoring()

    def run():
        local.bus = dbus.SystemBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            try:
                adapter.cycle(adapter.read(), threading.Event())
            except Exception as error:
                if not adapter.restore_pending:
                    errors.append(error)
            # Cross-connection power signals can invalidate a snapshot while
            # the worker reads it. The coordinator retries restoration only.
            deadline = time.monotonic() + 5
            while adapter.restore_pending and time.monotonic() < deadline:
                try:
                    adapter.restore()
                except RuntimeError:
                    if not adapter.restore_pending:
                        raise
                time.sleep(.001)
        except Exception as error:
            errors.append(error)
        finally:
            local.bus.close()

    worker = threading.Thread(target=run)
    worker.start()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    try:
        while worker.is_alive() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.001)
        worker.join(1)
        assert not worker.is_alive(), "isolated D-Bus cycle did not finish"
        assert not errors, (errors, writes)
        assert writes == [False, True]
        assert properties["Powered"]
        assert not adapter.restore_pending
        assert adapter._settings.read()[mod.BLUETOOTH_RESTORE_KEY] is None
    finally:
        adapter.stop_monitoring()
        server.release_name("org.bluez")
        server.close()
        monitor.close()
