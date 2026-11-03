"""SetNotificationPinned round trip on the isolated dbus-run-session test bus."""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from datetime import datetime, timezone

import dbus
import dbus.mainloop
import dbus.mainloop.glib
import dbus.service
import pytest
from gi.repository import GLib

from blueferry.ancs.events import AncsEvent
from blueferry.backend_operations import BackendDependencies
from blueferry.dbus_service import MessagesService
from blueferry.notification_log import NotificationLog
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, MESSAGES_IFACE, OBJECT_PATH

pytestmark = pytest.mark.private_dbus
_ids = itertools.count()
WHEN = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
ERROR = "io.weirdware.BlueFerry.Error."


class _Sessions:
    map = None
    pbap = None
    map_path = ""

    @staticmethod
    def report_error(_error) -> None:
        pass


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


def _call(name, method, *args):
    outcome: dict = {}

    def run() -> None:
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            proxy = dbus.Interface(
                connection.get_object(name, OBJECT_PATH, introspect=False), MESSAGES_IFACE,
            )
            outcome["value"] = getattr(proxy, method)(*args, timeout=5)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join()
    return outcome


@pytest.fixture
def service_factory():
    created = []

    def make(log):
        bus = dbus.SessionBus()
        name = f"{BUS_NAME}.Pinp{os.getpid()}n{next(_ids)}"
        bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
        service = MessagesService(
            bus_name, _Sessions(), BackendDependencies(notification_log=log),
        )
        if log is not None:
            log.set_listener(service.emit_notifications_changed)
        created.append((bus, name, service))
        return name

    yield make
    for bus, name, service in created:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def test_pin_round_trip_errors_and_content_free_signal(service_factory) -> None:
    log = NotificationLog(show_content=True)
    log.handle_ancs(AncsEvent(1, "com.example.chat", "Chat", "Title", "", "a", seen_at=WHEN))
    log.handle_ancs(AncsEvent(2, "com.example.chat", "Chat", "Title", "", "b", seen_at=WHEN))
    name = service_factory(log)
    listener = dbus.SessionBus(private=True, mainloop=dbus.mainloop.glib.DBusGMainLoop())
    received = []
    listener.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=EVENTS_IFACE, signal_name="NotificationsChanged",
    )
    try:
        listed = json.loads(_call(name, "ListNotifications", dbus.UInt32(10))["value"])
        assert listed["pins_max"] == 20
        older = listed["notifications"][1]
        assert older["id"] == 1 and older["pinned"] is False

        result = _call(name, "SetNotificationPinned", older["key"], True)
        assert result["value"] is True or result["value"] == 1
        _dispatch_until(lambda: received)
        assert received == [()]
        listed = json.loads(_call(name, "ListNotifications", dbus.UInt32(10))["value"])
        assert [record["id"] for record in listed["notifications"]] == [1, 2]
        assert listed["notifications"][0]["pinned"] is True

        unknown = _call(name, "SetNotificationPinned", "missing", True)
        assert unknown["error"].get_dbus_name() == ERROR + "InvalidArgs"
        assert not _call(name, "SetNotificationPinned", older["key"], False)["value"]
    finally:
        listener.close()


def test_pinning_is_not_ready_while_the_list_is_off(service_factory) -> None:
    name = service_factory(None)
    result = _call(name, "SetNotificationPinned", "k1", True)
    assert result["error"].get_dbus_name() == ERROR + "NotReady"
