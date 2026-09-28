"""NetworkManager and BlueZ tethering strategies against scripted fake buses."""
from __future__ import annotations

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
    assert bus.live_receivers() == 0  # the activation watch is released


def test_missing_profile_is_created_once_then_activated() -> None:
    bus = nm_bus(existing=False, state=2)
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")

    outcome = _run_connect(backend)

    assert bus.methods()[:3] == ["GetConnectionByUuid", "AddConnection", "ActivateConnection"]
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
    uuids = {other: "wired-uuid", ACTIVE: connection_uuid(MAC)}
    bus = nm_bus(handlers={
        ("org.freedesktop.DBus.Properties", "Get", NM, "ActiveConnections"):
            lambda _p, _a: (dbus.Array([other, ACTIVE], signature="o"),),
        ("org.freedesktop.DBus.Properties", "Get", NM_ACTIVE_IFACE, "Uuid"):
            lambda path, _a: (uuids[path],),
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


def test_backends_never_touch_the_shared_acl_link() -> None:
    bus = nm_bus(state=2)
    backend = NetworkManagerTether(lambda: bus, MAC, user=lambda: "alice")
    _run_connect(backend)
    backend.disconnect(lambda: None, lambda _token: None)
    bluez = bluez_bus({
        ("org.bluez.Network1", "Connect"): lambda _p, _a: ("bnep0",),
        ("org.bluez.Network1", "Disconnect"): lambda _p, _a: (),
    })
    fallback = BluezTether(lambda: bluez, DEVICE)
    _run_connect(fallback)
    fallback.disconnect(lambda: None, lambda _token: None)

    for call in bus.calls + bluez.calls:
        assert call[2] not in {
            "org.bluez.Device1", "org.bluez.Bearer.BREDR1", "org.bluez.Bearer.LE1",
        }
        assert call[3] not in {"ConnectProfile", "DisconnectProfile"}


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
