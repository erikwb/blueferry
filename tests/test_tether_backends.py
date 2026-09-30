"""NetworkManager and BlueZ tethering strategies against scripted fake buses."""
from __future__ import annotations

from pathlib import Path

import dbus
import dbus.exceptions
import pytest

from blueferry import tether
from blueferry.tether_backends import (
    CONNECTION_ID,
    NM,
    NM_ACTIVE_IFACE,
    BluezTether,
    NetworkManagerTether,
    choose_backend,
    connection_uuid,
    nm_connection_settings,
    nm_error_token,
)

MAC = "AA:BB:CC:DD:EE:01"
DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01"
PROFILE = "/org/freedesktop/NetworkManager/Settings/7"
ACTIVE = "/org/freedesktop/NetworkManager/ActiveConnection/3"
NM_DEVICE = "/org/freedesktop/NetworkManager/Devices/9"


def _error(name: str, message: str = "") -> dbus.exceptions.DBusException:
    return dbus.exceptions.DBusException(message, name=name)


class FakeBus:
    """Answers ``call_async`` from a script and delivers signals on demand.

    A handler returns the reply argument tuple or raises a DBusException. A
    method without a handler is held until the test answers it, so ordering
    and cancellation races are explicit.
    """

    def __init__(self, handlers=None) -> None:
        self.handlers = dict(handlers or {})
        self.calls: list[tuple] = []
        self.held: list[tuple] = []
        self.receivers: list[list] = []

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply_handler, error_handler, timeout=-1.0):
        # Tethering must never touch the ACL link the bearer supervisor owns.
        if (
            interface == "org.bluez.Device1"
            or interface.startswith("org.bluez.Bearer.")
            or method in {"ConnectProfile", "DisconnectProfile"}
        ):
            raise AssertionError(f"forbidden call {interface}.{method}")
        key = (interface, method) if interface != "org.freedesktop.DBus.Properties" \
            else (interface, method, args[0], args[1] if len(args) > 1 else None)
        self.calls.append((bus_name, path, interface, method, signature, args))
        handler = self.handlers.get(key)
        if handler is None:
            self.held.append((key, path, args, reply_handler, error_handler))
            return
        try:
            result = handler(path, args)
        except dbus.exceptions.DBusException as error:
            error_handler(error)
            return
        reply_handler(*result)

    def add_signal_receiver(self, handler, **kwargs):
        entry = [handler, kwargs, True]
        self.receivers.append(entry)

        class Match:
            def remove(self_inner) -> None:
                entry[2] = False

        return Match()

    def emit(self, signal_name: str, path: str, *args) -> None:
        for handler, kwargs, live in list(self.receivers):
            if live and kwargs.get("signal_name") == signal_name and kwargs.get("path") == path:
                handler(*args)

    def live_receivers(self) -> int:
        return sum(1 for entry in self.receivers if entry[2])

    def methods(self) -> list[str]:
        return [call[3] for call in self.calls]


def _props(values: dict):
    def get(_path, args):
        return (values[(args[0], args[1])],)
    return get


def nm_bus(*, existing: bool = True, state: int = 1, handlers=None) -> FakeBus:
    properties = {
        (NM_ACTIVE_IFACE, "State"): dbus.UInt32(state),
        (NM_ACTIVE_IFACE, "Devices"): dbus.Array([dbus.ObjectPath(NM_DEVICE)], signature="o"),
        ("org.freedesktop.NetworkManager.Device", "IpInterface"): dbus.String("bnep0"),
    }
    table = {
        ("org.freedesktop.NetworkManager.Settings", "GetConnectionByUuid"): (
            (lambda _p, _a: (dbus.ObjectPath(PROFILE),)) if existing else _raise(
                f"{NM}.Settings.InvalidConnection", "No connection with the UUID was found."
            )
        ),
        ("org.freedesktop.NetworkManager.Settings", "AddConnection"):
            lambda _p, _a: (dbus.ObjectPath(PROFILE),),
        (NM, "ActivateConnection"): lambda _p, _a: (dbus.ObjectPath(ACTIVE),),
        (NM, "DeactivateConnection"): lambda _p, _a: (),
        # No Bluetooth device known: profile resolution falls back.
        (NM, "GetAllDevices"): lambda _p, _a: (dbus.Array([], signature="o"),),
    }
    for (interface, prop), _value in properties.items():
        table[("org.freedesktop.DBus.Properties", "Get", interface, prop)] = _props(properties)
    table.update(handlers or {})
    return FakeBus(table)


def _raise(name: str, message: str = ""):
    def handler(_path, _args):
        raise _error(name, message)
    return handler


def _run_connect(backend):
    outcome: dict[str, object] = {}
    backend.connect(
        lambda interface: outcome.setdefault("connected", interface),
        lambda token: outcome.setdefault("error", token),
        lambda token, user: outcome.setdefault("lost", (token, user)),
    )
    return outcome


# ---- NetworkManager ---------------------------------------------------------


def test_profile_is_user_private_panu_without_autoconnect() -> None:
    settings = nm_connection_settings(MAC, "alice")

    connection = settings["connection"]
    assert connection["type"] == "bluetooth"
    assert bool(connection["autoconnect"]) is False
    assert list(connection["permissions"]) == ["user:alice"]
    assert connection["uuid"] == connection_uuid(MAC)
    assert connection["id"] == CONNECTION_ID
    assert settings["bluetooth"]["type"] == "panu"
    assert bytes(settings["bluetooth"]["bdaddr"]) == bytes.fromhex("AABBCCDDEE01")
    assert settings["bluetooth"]["bdaddr"].signature == "y"
    assert settings["ipv4"]["method"] == "auto"
    # The profile name must not carry the phone's name or address.
    assert MAC not in str(connection["id"])


def test_profile_uuid_is_stable_per_phone() -> None:
    assert connection_uuid(MAC) == connection_uuid(MAC.lower())
    assert connection_uuid(MAC) != connection_uuid("AA:BB:CC:DD:EE:02")


def test_existing_profile_is_activated_and_reports_the_interface_when_active() -> None:
    bus = nm_bus()
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")

    outcome = _run_connect(backend)
    assert outcome == {}  # NetworkManager is still activating (DHCP)
    assert "AddConnection" not in bus.methods()
    activate = next(call for call in bus.calls if call[3] == "ActivateConnection")
    assert activate[5] == (PROFILE, "/", "/")

    bus.emit("StateChanged", ACTIVE, dbus.UInt32(2), dbus.UInt32(1))

    assert outcome == {"connected": "bnep0"}
    # The watch stays to tell a deliberate deactivation from a lost link.
    assert bus.live_receivers() == 1


def test_missing_profile_is_created_once_then_activated() -> None:
    bus = nm_bus(existing=False, state=2)
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")

    outcome = _run_connect(backend)

    assert bus.methods()[:4] == [
        "GetAllDevices", "GetConnectionByUuid", "AddConnection", "ActivateConnection",
    ]
    added = next(call for call in bus.calls if call[3] == "AddConnection")
    assert added[4] == "a{sa{sv}}"
    assert list(added[5][0]["connection"]["permissions"]) == ["user:alice"]
    # Already activated when the watch was installed: the State read covers it.
    assert outcome == {"connected": "bnep0"}


@pytest.mark.parametrize(("reason", "token"), [
    (1, tether.ACTIVATION_FAILED),   # NONE: the phone refused, e.g. hotspot off
    (3, tether.ACTIVATION_FAILED),   # DEVICE_DISCONNECTED
    (5, tether.IP_CONFIG_FAILED),
    (6, tether.TIMEOUT),
    (14, tether.NO_NETWORK_DEVICE),
])
def test_deactivation_during_activation_maps_the_reason(reason, token) -> None:
    bus = nm_bus()
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    outcome = _run_connect(backend)

    bus.emit("StateChanged", ACTIVE, dbus.UInt32(4), dbus.UInt32(reason))

    assert outcome == {"error": token}
    assert bus.live_receivers() == 0


@pytest.mark.parametrize(("method", "name", "token"), [
    ("ActivateConnection", f"{NM}.PermissionDenied", tether.PERMISSION_DENIED),
    ("ActivateConnection", f"{NM}.UnknownDevice", tether.NO_NETWORK_DEVICE),
    ("ActivateConnection", f"{NM}.ConnectionNotAvailable", tether.NO_NETWORK_DEVICE),
    ("AddConnection", f"{NM}.Settings.PermissionDenied", tether.PERMISSION_DENIED),
    ("AddConnection", f"{NM}.Settings.InvalidProperty", tether.NOT_SUPPORTED),
    ("GetConnectionByUuid", "org.freedesktop.DBus.Error.ServiceUnknown",
     tether.NETWORKMANAGER_UNAVAILABLE),
    ("ActivateConnection", "org.freedesktop.DBus.Error.NoReply", tether.TIMEOUT),
    ("ActivateConnection", "org.example.Unexpected", tether.GENERIC_ERROR),
])
def test_networkmanager_errors_map_to_public_tokens(method, name, token) -> None:
    interface = NM if method == "ActivateConnection" else "org.freedesktop.NetworkManager.Settings"
    bus = nm_bus(existing=method != "AddConnection", handlers={
        (interface, method): _raise(name, "Joshua's iPhone is not available"),
    })
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")

    assert _run_connect(backend) == {"error": token}


def test_error_token_mapping_is_name_based() -> None:
    assert nm_error_token(_error(f"{NM}.Settings.PermissionDenied")) == tether.PERMISSION_DENIED
    assert nm_error_token(RuntimeError("x")) == tether.GENERIC_ERROR


def test_disconnect_deactivates_the_known_activation() -> None:
    bus = nm_bus(state=2)
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    _run_connect(backend)
    done = []

    backend.disconnect(lambda: done.append(True), lambda token: done.append(token))

    deactivate = next(call for call in bus.calls if call[3] == "DeactivateConnection")
    assert deactivate[5] == (ACTIVE,)
    assert done == [True]


def test_disconnect_of_an_inactive_profile_succeeds() -> None:
    bus = nm_bus(state=2, handlers={
        (NM, "DeactivateConnection"): _raise(f"{NM}.ConnectionNotActive"),
    })
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    _run_connect(backend)
    done = []
    backend.disconnect(lambda: done.append(True), done.append)
    assert done == [True]


def test_disconnect_after_restart_finds_blueferrys_activation_by_uuid() -> None:
    other = "/org/freedesktop/NetworkManager/ActiveConnection/1"
    active = {
        other: {"Uuid": "wired-uuid", "Type": "802-3-ethernet", "Devices": ["/Devices/1"]},
        ACTIVE: {"Uuid": connection_uuid(MAC), "Type": "bluetooth", "Devices": []},
    }
    bus = nm_bus(handlers={
        ("org.freedesktop.DBus.Properties", "Get", NM, "ActiveConnections"):
            lambda _p, _a: (dbus.Array([other, ACTIVE], signature="o"),),
        ("org.freedesktop.DBus.Properties", "GetAll", NM_ACTIVE_IFACE, None):
            lambda path, _a: (active[path],),
    })
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    done = []

    backend.disconnect(lambda: done.append(True), done.append)

    deactivations = [call[5] for call in bus.calls if call[3] == "DeactivateConnection"]
    assert deactivations == [(ACTIVE,)]  # never the unrelated wired connection
    assert done == [True]


def test_disconnect_without_an_activation_is_a_no_op() -> None:
    bus = nm_bus(handlers={
        ("org.freedesktop.DBus.Properties", "Get", NM, "ActiveConnections"):
            lambda _p, _a: (dbus.Array([], signature="o"),),
    })
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    done = []
    backend.disconnect(lambda: done.append(True), done.append)
    assert "DeactivateConnection" not in bus.methods()
    assert done == [True]


def test_activation_that_completes_after_cancel_is_withdrawn() -> None:
    bus = nm_bus()
    del bus.handlers[(NM, "ActivateConnection")]  # hold the reply
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    outcome = _run_connect(backend)

    backend.cancel()
    (_key, _path, _args, reply, _error_handler) = bus.held.pop()
    reply(dbus.ObjectPath(ACTIVE))

    assert outcome == {}
    assert [call[5] for call in bus.calls if call[3] == "DeactivateConnection"] == [(ACTIVE,)]
    assert bus.live_receivers() == 0


def test_cancelled_attempt_ignores_late_state_signals() -> None:
    bus = nm_bus()
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    outcome = _run_connect(backend)
    backend.cancel()
    bus.emit("StateChanged", ACTIVE, dbus.UInt32(2), dbus.UInt32(1))
    assert outcome == {}


# ---- established tether ending -----------------------------------------------


@pytest.mark.parametrize(("reason", "expected"), [
    (2, (tether.LINK_LOST, True)),       # USER_DISCONNECTED, e.g. the applet
    (3, (tether.LINK_LOST, False)),      # DEVICE_DISCONNECTED
    (5, (tether.IP_CONFIG_FAILED, False)),
])
def test_end_of_an_established_tether_is_reported_with_its_cause(reason, expected) -> None:
    bus = nm_bus(state=2)
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    outcome = _run_connect(backend)
    assert outcome == {"connected": "bnep0"}

    bus.emit("StateChanged", ACTIVE, dbus.UInt32(4), dbus.UInt32(reason))

    assert outcome["lost"] == expected
    assert "error" not in outcome
    assert bus.live_receivers() == 0


# ---- reusing an existing PAN profile ------------------------------------------

BT_DEVICE = "/org/freedesktop/NetworkManager/Devices/6"
FOREIGN = "/org/freedesktop/NetworkManager/Settings/2"
DUN = "/org/freedesktop/NetworkManager/Settings/3"
OWN = "/org/freedesktop/NetworkManager/Settings/4"
NEWER = "/org/freedesktop/NetworkManager/Settings/5"


def _profile(uuid_: str, kind: str = "panu", *, stamp: int = 0, bdaddr=None, name="x"):
    bluetooth = {"type": kind}
    if bdaddr is not None:
        bluetooth["bdaddr"] = bdaddr
    return {
        "connection": {"id": name, "uuid": uuid_, "type": "bluetooth", "timestamp": stamp},
        "bluetooth": bluetooth,
    }


def world(profiles: dict, *, hwaddr: str = MAC.lower(), device_type: int = 5,
          devices=(BT_DEVICE,), handlers=None) -> FakeBus:
    table = {
        (NM, "GetAllDevices"): lambda _p, _a: (dbus.Array(list(devices), signature="o"),),
        ("org.freedesktop.DBus.Properties", "GetAll", "org.freedesktop.NetworkManager.Device",
         None): lambda _p, _a: ({
            "DeviceType": dbus.UInt32(device_type),
            "AvailableConnections": dbus.Array(list(profiles), signature="o"),
        },),
        ("org.freedesktop.DBus.Properties", "Get",
         "org.freedesktop.NetworkManager.Device.Bluetooth", "HwAddress"):
            lambda _p, _a: (hwaddr,),
        ("org.freedesktop.NetworkManager.Settings.Connection", "GetSettings"):
            lambda path, _a: (profiles[path],),
    }
    table.update(handlers or {})
    return nm_bus(existing=False, state=2, handlers=table)


def _activated(bus: FakeBus) -> list:
    return [call[5][0] for call in bus.calls if call[3] == "ActivateConnection"]


def test_foreign_panu_profile_is_reused_without_creating_one() -> None:
    bus = world({FOREIGN: _profile("foreign-uuid", bdaddr=MAC, name="Joshua's iPhone Network")})
    outcome = _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))

    assert outcome == {"connected": "bnep0"}
    assert _activated(bus) == [FOREIGN]
    assert "AddConnection" not in bus.methods()
    assert "GetConnectionByUuid" not in bus.methods()
    # A foreign profile is never modified or removed.
    assert not {"Update", "Update2", "Delete"} & set(bus.methods())


def test_dun_only_profile_is_not_reused() -> None:
    bus = world({DUN: _profile("dun-uuid", "dun")})
    _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))

    assert "AddConnection" in bus.methods()
    assert _activated(bus) == [PROFILE]


def test_own_profile_is_preferred_over_a_newer_foreign_one() -> None:
    bus = world({
        FOREIGN: _profile("foreign-uuid", stamp=2_000_000_000),
        OWN: _profile(connection_uuid(MAC), stamp=1),
    })
    _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert _activated(bus) == [OWN]


def test_most_recently_used_foreign_profile_wins() -> None:
    bus = world({
        FOREIGN: _profile("old-uuid", stamp=10),
        NEWER: _profile("new-uuid", stamp=20, bdaddr=dbus.Array(
            [dbus.Byte(b) for b in bytes.fromhex("AABBCCDDEE01")], signature="y")),
    })
    _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert _activated(bus) == [NEWER]


def test_profile_for_another_phone_is_ignored() -> None:
    bus = world({FOREIGN: _profile("other-uuid", bdaddr="AA:BB:CC:DD:EE:99")})
    _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert "AddConnection" in bus.methods()


@pytest.mark.parametrize("variant", ["no-device", "other-address", "not-bluetooth"])
def test_without_a_matching_device_the_own_profile_is_created(variant) -> None:
    kwargs = {
        "no-device": {"devices": ()},
        "other-address": {"hwaddr": "11:22:33:44:55:66"},
        "not-bluetooth": {"device_type": 1},
    }[variant]
    bus = world({FOREIGN: _profile("foreign-uuid")}, **kwargs)
    _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert "AddConnection" in bus.methods()


def test_failed_resolution_falls_back_to_the_own_profile() -> None:
    bus = world({}, handlers={(NM, "GetAllDevices"): _raise(f"{NM}.PermissionDenied")})
    outcome = _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert outcome == {"connected": "bnep0"}
    assert "AddConnection" in bus.methods()


def test_profile_names_are_never_logged(caplog) -> None:
    bus = world({FOREIGN: _profile("foreign-uuid", name="Joshua's iPhone Network")})
    with caplog.at_level("DEBUG"):
        _run_connect(NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice"))
    assert "Joshua" not in caplog.text
    assert MAC not in caplog.text.upper()


# ---- stopping a tether BlueFerry did not start -----------------------------------

FOREIGN_ACTIVE = "/org/freedesktop/NetworkManager/ActiveConnection/8"
WIRED_ACTIVE = "/org/freedesktop/NetworkManager/ActiveConnection/1"


def _active_world(active: dict, hwaddrs: dict) -> FakeBus:
    return nm_bus(handlers={
        ("org.freedesktop.DBus.Properties", "Get", NM, "ActiveConnections"):
            lambda _p, _a: (dbus.Array(list(active), signature="o"),),
        ("org.freedesktop.DBus.Properties", "GetAll", NM_ACTIVE_IFACE, None):
            lambda path, _a: (active[path],),
        ("org.freedesktop.DBus.Properties", "Get",
         "org.freedesktop.NetworkManager.Device.Bluetooth", "HwAddress"):
            lambda path, _a: (hwaddrs[path],),
    })


def _deactivated(bus: FakeBus) -> list:
    return [call[5] for call in bus.calls if call[3] == "DeactivateConnection"]


def test_adopted_applet_tether_is_found_by_the_phone_address() -> None:
    bus = _active_world({
        WIRED_ACTIVE: {"Uuid": "wired", "Type": "802-3-ethernet", "Devices": ["/Devices/1"]},
        FOREIGN_ACTIVE: {"Uuid": "foreign-panu", "Type": "bluetooth", "Devices": [BT_DEVICE]},
    }, {BT_DEVICE: MAC.lower()})
    done = []

    NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice").disconnect(
        lambda: done.append(True), done.append,
    )

    assert _deactivated(bus) == [(FOREIGN_ACTIVE,)]
    assert done == [True]


def test_controller_stops_an_adopted_applet_tether_end_to_end() -> None:
    from blueferry.tether import TetherController

    bus = _active_world({
        FOREIGN_ACTIVE: {"Uuid": "foreign-panu", "Type": "bluetooth", "Devices": [BT_DEVICE]},
    }, {BT_DEVICE: MAC})
    probes = []
    link = type("Link", (), {
        "start": lambda self: None, "stop": lambda self: None,
        "probe": lambda self: probes.append(True),
    })()
    controller = TetherController(
        lambda on_backend, _on_error: on_backend(
            NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
        ),
        link_watch=link,
        schedule=lambda _seconds, _callback: 1,
        cancel=lambda _source: None,
    )
    controller.start()
    controller.observe_link(True, "bnep0")

    controller.disconnect()
    assert _deactivated(bus) == [(FOREIGN_ACTIVE,)]
    assert controller.state == "disconnecting"  # until BlueZ confirms
    controller.observe_link(False, "")
    assert controller.snapshot()["state"] == "off"
    assert controller.snapshot()["error"] == ""


def test_bluetooth_tether_of_another_phone_is_left_alone() -> None:
    bus = _active_world({
        FOREIGN_ACTIVE: {"Uuid": "foreign-panu", "Type": "bluetooth", "Devices": [BT_DEVICE]},
    }, {BT_DEVICE: "11:22:33:44:55:66"})
    done = []
    NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice").disconnect(
        lambda: done.append(True), done.append,
    )
    assert _deactivated(bus) == []
    assert done == [True]


def test_reused_profile_uuid_is_recognised_when_stopping() -> None:
    bus = world({FOREIGN: _profile("foreign-uuid")})
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    _run_connect(backend)
    backend.cancel()
    backend._active_path = None  # as after losing track of the activation
    bus.handlers[("org.freedesktop.DBus.Properties", "Get", NM, "ActiveConnections")] = (
        lambda _p, _a: (dbus.Array([FOREIGN_ACTIVE], signature="o"),)
    )
    bus.handlers[("org.freedesktop.DBus.Properties", "GetAll", NM_ACTIVE_IFACE, None)] = (
        lambda _p, _a: ({"Uuid": "foreign-uuid", "Type": "bluetooth", "Devices": []},)
    )

    backend.disconnect(lambda: None, lambda _token: None)

    assert _deactivated(bus) == [(FOREIGN_ACTIVE,)]


# ---- BlueZ fallback -------------------------------------------------------------


def bluez_bus(handlers) -> FakeBus:
    return FakeBus(handlers)


def test_bluez_connect_requests_the_nap_role_and_reports_the_interface() -> None:
    bus = bluez_bus({("org.bluez.Network1", "Connect"): lambda _p, _a: ("bnep0",)})
    outcome = _run_connect(BluezTether(lambda: bus, DEVICE))

    assert bus.calls[0][:6] == ("org.bluez", DEVICE, "org.bluez.Network1", "Connect", "s", ("nap",))
    assert outcome == {"connected": "bnep0"}


@pytest.mark.parametrize(("name", "message", "token"), [
    ("org.bluez.Error.Failed", "Connection refused (111)", tether.HOTSPOT_REFUSED),
    ("org.bluez.Error.NotSupported", "", tether.NOT_SUPPORTED),
    ("org.freedesktop.DBus.Error.UnknownMethod", "", tether.NOT_SUPPORTED),
    ("org.bluez.Error.Failed", "Host is down (112)", tether.PHONE_UNREACHABLE),
])
def test_bluez_connect_errors_are_mapped(name, message, token) -> None:
    bus = bluez_bus({("org.bluez.Network1", "Connect"): _raise(name, message)})
    assert _run_connect(BluezTether(lambda: bus, DEVICE)) == {"error": token}


def test_bluez_already_connected_reads_the_existing_interface() -> None:
    bus = bluez_bus({
        ("org.bluez.Network1", "Connect"): _raise("org.bluez.Error.AlreadyConnected"),
        ("org.freedesktop.DBus.Properties", "Get", "org.bluez.Network1", "Interface"):
            lambda _p, _a: ("bnep1",),
    })
    assert _run_connect(BluezTether(lambda: bus, DEVICE)) == {"connected": "bnep1"}


def test_bluez_disconnect_only_touches_network1() -> None:
    bus = bluez_bus({("org.bluez.Network1", "Disconnect"): lambda _p, _a: ()})
    done = []
    BluezTether(lambda: bus, DEVICE).disconnect(lambda: done.append(True), done.append)

    assert done == [True]
    assert [(call[2], call[3]) for call in bus.calls] == [("org.bluez.Network1", "Disconnect")]


def test_bluez_disconnect_when_not_connected_succeeds() -> None:
    bus = bluez_bus({
        ("org.bluez.Network1", "Disconnect"): _raise("org.bluez.Error.NotConnected"),
    })
    done = []
    BluezTether(lambda: bus, DEVICE).disconnect(lambda: done.append(True), done.append)
    assert done == [True]


def test_bluez_cancel_drops_a_late_reply() -> None:
    bus = bluez_bus({})
    backend = BluezTether(lambda: bus, DEVICE)
    outcome = _run_connect(backend)
    backend.cancel()
    bus.held.pop()[3]("bnep0")
    assert outcome == {}


def test_forbidden_calls_fail_the_fake_bus() -> None:
    bus = FakeBus()
    for interface, method in (
        ("org.bluez.Device1", "Connect"),
        ("org.bluez.Bearer.BREDR1", "Disconnect"),
        ("org.bluez.Device1", "ConnectProfile"),
        ("org.bluez.Device1", "DisconnectProfile"),
    ):
        with pytest.raises(AssertionError, match="forbidden"):
            bus.call_async("org.bluez", DEVICE, interface, method, "", (),
                           lambda *_a: None, lambda _e: None)


def test_tether_sources_never_name_link_level_methods() -> None:
    sources = sorted((Path(__file__).resolve().parents[1] / "src/blueferry").glob("tether*.py"))
    assert len(sources) >= 3
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for forbidden in ("org.bluez.Device1", "org.bluez.Bearer", "ConnectProfile"):
            assert forbidden not in text, (source.name, forbidden)


# ---- backend selection ---------------------------------------------------------


@pytest.mark.parametrize(("owned", "expected"), [
    (True, "networkmanager"),
    (False, "bluez"),
])
def test_auto_prefers_networkmanager_when_it_owns_its_name(owned, expected) -> None:
    bus = FakeBus({("org.freedesktop.DBus", "NameHasOwner"): lambda _p, args: (
        owned and args == (NM,),
    )})
    chosen = []
    choose_backend(lambda: bus, DEVICE, MAC)(chosen.append, chosen.append)
    assert [backend.name for backend in chosen] == [expected]


@pytest.mark.parametrize("mode", ["networkmanager", "bluez"])
def test_explicit_backend_mode_skips_the_probe(mode) -> None:
    bus = FakeBus()
    chosen = []
    choose_backend(lambda: bus, DEVICE, MAC, mode)(chosen.append, chosen.append)
    assert [backend.name for backend in chosen] == [mode]
    assert bus.calls == []


def test_failed_probe_is_reported_as_a_token() -> None:
    bus = FakeBus({("org.freedesktop.DBus", "NameHasOwner"): _raise(
        "org.freedesktop.DBus.Error.NoReply",
    )})
    errors = []
    choose_backend(lambda: bus, DEVICE, MAC)(lambda _b: None, errors.append)
    assert errors == [tether.GENERIC_ERROR]
