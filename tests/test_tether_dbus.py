"""Tether1 over an isolated session bus: contract, errors, and signal privacy."""
from __future__ import annotations

import itertools
import json
import os
import threading
import time

import dbus
import dbus.mainloop
import dbus.service
import pytest
from gi.repository import GLib

from blueferry.backend_operations import BackendDependencies
from blueferry.client import BackendClient, BackendError
from blueferry.dbus_service import MessagesService
from blueferry.protocol import BUS_NAME, ERROR_PREFIX, OBJECT_PATH, TETHER_IFACE
from blueferry.tether import TetherController

pytestmark = pytest.mark.private_dbus
_service_ids = itertools.count()


class _Sessions:
    map = object()
    pbap = object()
    map_path = "/session/map"

    @staticmethod
    def report_error(_error) -> None:
        pass


class _Backend:
    name = "networkmanager"

    def __init__(self) -> None:
        self.connects: list = []
        self.disconnects: list = []

    def connect(self, on_connected, on_error, on_lost) -> None:
        self.connects.append((on_connected, on_error, on_lost))

    def disconnect(self, on_done, on_error) -> None:
        self.disconnects.append((on_done, on_error))

    def cancel(self) -> None:
        pass


def _service(tether, set_tethering=None):
    bus = dbus.SessionBus()
    name = f"{BUS_NAME}.Tethert{os.getpid()}n{next(_service_ids)}"
    bus_name = dbus.service.BusName(name, bus=bus, do_not_queue=True)
    service = MessagesService(
        bus_name,
        _Sessions(),
        BackendDependencies(
            status_provider=lambda: {"initializing": False},
            tether=tether,
            set_tethering=set_tethering,
        ),
    )
    return bus, name, service


@pytest.fixture
def tether_service():
    backend = _Backend()
    classic = {"up": True}
    holder: dict = {}
    controller = TetherController(
        lambda on_backend, _on_error: on_backend(backend),
        classic_ready=lambda: classic["up"],
        on_changed=lambda: holder["service"].emit_tether_changed(),
        enabled=True,
        schedule=lambda _seconds, _callback: 1,
        cancel=lambda _source: None,
    )
    bus, name, service = _service(controller, controller.configure)
    holder["service"] = service
    try:
        yield name, controller, backend, classic, service
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)


def _dispatch_until(predicate, *, timeout: float = 5.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    assert predicate(), "timed out waiting for D-Bus dispatch"


def _in_thread(name: str, work):
    outcome: dict = {}

    def run() -> None:
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            proxy = connection.get_object(name, OBJECT_PATH)
            outcome["value"] = work(proxy)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    _dispatch_until(lambda: not thread.is_alive())
    thread.join(timeout=1)
    return outcome


def _tether(proxy):
    return dbus.Interface(proxy, TETHER_IFACE)


def test_connect_get_state_and_disconnect_round_trip(tether_service) -> None:
    name, _controller, backend, _classic, _service = tether_service

    outcome = _in_thread(name, lambda proxy: json.loads(str(_tether(proxy).Connect(timeout=5))))
    assert outcome["value"]["state"] == "connecting"

    backend.connects[0][0]("bnep0")
    state = _in_thread(name, lambda proxy: json.loads(str(_tether(proxy).GetState(timeout=5))))
    assert state["value"] == {
        "state": "connected", "interface": "bnep0", "backend": "networkmanager",
        "external": False, "error": "", "needs_dhcp": False, "enabled": True,
        "autoconnect": False,
    }

    stopped = _in_thread(name, lambda proxy: json.loads(str(_tether(proxy).Disconnect(timeout=5))))
    assert stopped["value"]["state"] == "disconnecting"
    backend.disconnects[0][0]()
    final = _in_thread(name, lambda proxy: json.loads(str(_tether(proxy).GetState(timeout=5))))
    assert final["value"]["state"] == "off"


def test_connect_without_a_classic_link_is_a_stable_not_ready_error(tether_service) -> None:
    name, _controller, backend, classic, _service = tether_service
    classic["up"] = False

    outcome = _in_thread(name, lambda proxy: _tether(proxy).Connect(timeout=5))

    error = outcome["error"]
    assert isinstance(error, dbus.exceptions.DBusException)
    assert error.get_dbus_name() == f"{ERROR_PREFIX}.NotReady"
    assert backend.connects == []


def test_tether_changed_is_content_free(tether_service) -> None:
    name, controller, backend, _classic, _service = tether_service
    connection = dbus.SessionBus(private=True)
    received: list[tuple] = []
    match = connection.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=TETHER_IFACE,
        signal_name="TetherChanged",
        bus_name=name,
        path=OBJECT_PATH,
    )
    try:
        controller.connect()
        backend.connects[0][0]("bnep0")
        _dispatch_until(lambda: len(received) >= 2)
    finally:
        match.remove()
        connection.close()

    assert all(args == () for args in received)


def test_tether_commands_are_rate_limited(tether_service) -> None:
    name, *_ = tether_service

    def hammer(proxy):
        interface = _tether(proxy)
        for _ in range(10):
            interface.Disconnect(timeout=5)
        interface.Disconnect(timeout=5)

    outcome = _in_thread(name, hammer)

    assert outcome["error"].get_dbus_name() == f"{ERROR_PREFIX}.RateLimited"
    # Status reads use their own bucket and keep working.
    state = _in_thread(name, lambda proxy: str(_tether(proxy).GetState(timeout=5)))
    assert "value" in state


def test_backend_without_tethering_reports_not_ready() -> None:
    bus, name, service = _service(None)
    try:
        outcome = _in_thread(name, lambda proxy: _tether(proxy).GetState(timeout=5))
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert outcome["error"].get_dbus_name() == f"{ERROR_PREFIX}.NotReady"


def test_python_client_round_trip(tether_service) -> None:
    name, _controller, backend, _classic, _service = tether_service

    def run(_proxy):
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = BackendClient(
                interface_factory=lambda iface: dbus.Interface(
                    connection.get_object(name, OBJECT_PATH), iface,
                )
            )
            started = client.tether_connect()
            state = client.tether_state()
            return started, state
        finally:
            connection.close()

    outcome = _in_thread(name, run)
    started, state = outcome["value"]
    assert started.state == "connecting"
    assert state.state == "connecting"
    assert backend.connects


def test_python_client_maps_dbus_errors(tether_service) -> None:
    name, _controller, _backend, classic, _service = tether_service
    classic["up"] = False

    def run(_proxy):
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = BackendClient(
                interface_factory=lambda iface: dbus.Interface(
                    connection.get_object(name, OBJECT_PATH), iface,
                )
            )
            client.tether_connect()
        finally:
            connection.close()

    outcome = _in_thread(name, run)
    assert isinstance(outcome["error"], BackendError)
    assert "not connected over Bluetooth" in str(outcome["error"])


def test_set_tethering_off_refuses_connect_over_the_bus(tether_service) -> None:
    name, controller, backend, _classic, _service = tether_service

    def run(proxy):
        interface = _tether(proxy)
        saved = json.loads(str(interface.SetTethering(False, False, timeout=5)))
        try:
            interface.Connect(timeout=5)
        except dbus.exceptions.DBusException as error:
            return saved, error
        return saved, None

    outcome = _in_thread(name, run)
    saved, error = outcome["value"]
    assert saved["enabled"] is False
    assert controller.enabled is False
    assert error is not None
    assert error.get_dbus_name() == f"{ERROR_PREFIX}.NotReady"
    assert "turned off" in error.get_dbus_message()
    assert backend.connects == []


def test_set_tethering_disable_stops_our_link_and_emits_only_the_signal(
    tether_service,
) -> None:
    name, controller, backend, _classic, _service = tether_service
    controller.connect()
    backend.connects[0][0]("bnep0")
    connection = dbus.SessionBus(private=True)
    received: list[tuple] = []
    match = connection.add_signal_receiver(
        lambda *args: received.append(args),
        dbus_interface=TETHER_IFACE,
        signal_name="TetherChanged",
        bus_name=name,
        path=OBJECT_PATH,
    )
    try:
        outcome = _in_thread(
            name, lambda proxy: json.loads(str(_tether(proxy).SetTethering(False, False))),
        )
        _dispatch_until(lambda: len(received) >= 1)
    finally:
        match.remove()
        connection.close()

    assert outcome["value"]["state"] == "disconnecting"
    assert outcome["value"]["enabled"] is False
    assert len(backend.disconnects) == 1
    assert all(args == () for args in received)


def test_set_tethering_uses_the_settings_rate_bucket(tether_service) -> None:
    name, *_ = tether_service

    def toggle(proxy):
        interface = _tether(proxy)
        for _ in range(10):
            interface.Disconnect(timeout=5)
        # The tether bucket is exhausted; settings still work.
        return json.loads(str(interface.SetTethering(True, True, timeout=5)))

    outcome = _in_thread(name, toggle)
    assert outcome["value"]["autoconnect"] is True


def test_backend_without_the_setter_reports_not_ready() -> None:
    bus, name, service = _service(None)
    try:
        outcome = _in_thread(name, lambda proxy: _tether(proxy).SetTethering(True, False))
    finally:
        service.close()
        service.remove_from_connection()
        bus.release_name(name)

    assert outcome["error"].get_dbus_name() == f"{ERROR_PREFIX}.NotReady"


def test_python_client_saves_the_opt_in(tether_service) -> None:
    name, controller, _backend, _classic, _service = tether_service

    def run(_proxy):
        connection = dbus.SessionBus(private=True, mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = BackendClient(
                interface_factory=lambda iface: dbus.Interface(
                    connection.get_object(name, OBJECT_PATH), iface,
                )
            )
            return client.tether_configure(False, True)
        finally:
            connection.close()

    outcome = _in_thread(name, run)
    status = outcome["value"]
    assert (status.enabled, status.autoconnect) == (False, True)
    assert controller.enabled is False
