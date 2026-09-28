"""Tethering state machine, link reconciliation, and error mapping (no D-Bus)."""
from __future__ import annotations

import json

import dbus.exceptions
import pytest

from blueferry import tether
from blueferry.errors import NotReadyError
from blueferry.tether import (
    CONNECTED,
    CONNECTING,
    DISCONNECTING,
    FAILED,
    OFF,
    NetworkLinkWatch,
    TetherController,
    bluez_error_token,
)


class Timers:
    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, object]] = {}
        self._next = 0

    def schedule(self, seconds, callback) -> int:
        self._next += 1
        self.pending[self._next] = (seconds, callback)
        return self._next

    def cancel(self, source_id) -> None:
        self.pending.pop(source_id, None)

    def fire(self, predicate=lambda _seconds: True) -> None:
        for source_id, (seconds, callback) in list(self.pending.items()):
            if predicate(seconds):
                self.pending.pop(source_id, None)
                callback()

    def delays(self) -> list[int]:
        return sorted(seconds for seconds, _ in self.pending.values())


class Backend:
    """Records requests; the test decides when and how each completes."""

    def __init__(self, name: str = "networkmanager") -> None:
        self.name = name
        self.connects: list[tuple] = []
        self.disconnects: list[tuple] = []
        self.cancelled = 0

    def connect(self, on_connected, on_error) -> None:
        self.connects.append((on_connected, on_error))

    def disconnect(self, on_done, on_error) -> None:
        self.disconnects.append((on_done, on_error))

    def cancel(self) -> None:
        self.cancelled += 1


class Chooser:
    def __init__(self, backend: Backend | None = None, *, fail: str | None = None) -> None:
        self.backend = backend or Backend()
        self.fail = fail
        self.calls = 0

    def __call__(self, on_backend, on_error) -> None:
        self.calls += 1
        if self.fail:
            on_error(self.fail)
        else:
            on_backend(self.backend)


def controller(chooser=None, *, classic=True, ready=True, autoconnect=False, link=None):
    timers = Timers()
    changes: list[None] = []
    flags = {"classic": classic, "ready": ready}
    value = TetherController(
        chooser or Chooser(),
        link_watch=link,
        classic_ready=lambda: flags["classic"],
        autoconnect_ready=lambda: flags["ready"],
        on_changed=lambda: changes.append(None),
        autoconnect=autoconnect,
        schedule=timers.schedule,
        cancel=timers.cancel,
    )
    return value, timers, changes, flags


def test_default_state_is_off_and_never_connects_by_itself() -> None:
    chooser = Chooser()
    value, timers, changes, _ = controller(chooser)
    value.start()
    value.maybe_autoconnect()

    assert value.snapshot() == {
        "state": OFF, "interface": "", "backend": "", "external": False,
        "error": "", "needs_dhcp": False, "autoconnect": False,
    }
    assert chooser.calls == 0
    assert timers.pending == {}
    assert changes == []


def test_connect_success_reports_interface_and_backend() -> None:
    chooser = Chooser()
    value, timers, changes, _ = controller(chooser)

    started = value.connect()
    assert started["state"] == CONNECTING
    assert timers.delays() == [tether.CONNECT_DEADLINE_SECONDS]

    on_connected, _ = chooser.backend.connects[0]
    on_connected("bnep0")

    assert value.snapshot()["state"] == CONNECTED
    assert value.snapshot()["interface"] == "bnep0"
    assert value.snapshot()["backend"] == "networkmanager"
    assert value.snapshot()["needs_dhcp"] is False
    assert timers.pending == {}  # the deadline is withdrawn
    assert changes  # every transition is announced


def test_bluez_fallback_tells_the_user_to_run_dhcp() -> None:
    chooser = Chooser(Backend("bluez"))
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("bnep0")

    assert value.snapshot()["needs_dhcp"] is True
    assert value.snapshot()["interface"] == "bnep0"


def test_connect_requires_the_existing_classic_link() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser, classic=False)

    with pytest.raises(NotReadyError, match="not connected over Bluetooth"):
        value.connect()
    assert chooser.calls == 0
    assert value.state == OFF


def test_repeated_connect_is_idempotent() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    value.connect()
    assert len(chooser.backend.connects) == 1


@pytest.mark.parametrize("token", [
    tether.HOTSPOT_REFUSED, tether.NOT_SUPPORTED, tether.PERMISSION_DENIED,
])
def test_backend_failure_is_reported_as_a_token(token) -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][1](token)

    assert value.snapshot()["state"] == FAILED
    assert value.snapshot()["error"] == token
    assert timers.pending == {}  # no retry without the autoconnect opt-in


def test_unknown_backend_token_is_normalized() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][1]("org.example.Private: Joshua's iPhone")

    assert value.snapshot()["error"] == tether.GENERIC_ERROR


def test_backend_choice_failure_fails_the_attempt() -> None:
    value, *_ = controller(Chooser(fail=tether.GENERIC_ERROR))
    value.connect()
    assert value.snapshot()["state"] == FAILED


def test_deadline_fails_and_withdraws_a_hanging_attempt() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser)
    value.connect()

    timers.fire()

    assert value.snapshot()["state"] == FAILED
    assert value.snapshot()["error"] == tether.TIMEOUT
    assert len(chooser.backend.disconnects) == 1
    # A reply that finally arrives cannot resurrect the attempt.
    chooser.backend.connects[0][0]("bnep0")
    assert value.snapshot()["state"] == FAILED


def test_disconnect_while_connecting_supersedes_the_attempt() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser)
    value.connect()

    assert value.disconnect()["state"] == DISCONNECTING
    late_success, _ = chooser.backend.connects[0]
    late_success("bnep0")
    assert value.state == DISCONNECTING

    chooser.backend.disconnects[0][0]()
    assert value.snapshot()["state"] == OFF
    assert timers.pending == {}


def test_disconnect_when_off_only_clears_a_stale_failure() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][1](tether.HOTSPOT_REFUSED)

    assert value.disconnect() == {**value.snapshot(), "state": OFF, "error": ""}
    assert chooser.backend.disconnects == []


def test_connect_while_disconnecting_is_not_ready() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("bnep0")
    value.disconnect()

    with pytest.raises(NotReadyError, match="disconnecting"):
        value.connect()


def test_failed_disconnect_is_reported_and_rechecks_the_link() -> None:
    probes = []
    link = type("Link", (), {
        "start": lambda self: None, "stop": lambda self: None,
        "probe": lambda self: probes.append(True),
    })()
    chooser = Chooser()
    value, *_ = controller(chooser, link=link)
    value.start()
    value.connect()
    chooser.backend.connects[0][0]("bnep0")
    value.disconnect()
    chooser.backend.disconnects[0][1](tether.PERMISSION_DENIED)

    assert value.snapshot()["state"] == FAILED
    assert value.snapshot()["error"] == tether.PERMISSION_DENIED
    assert probes == [True]


def test_lost_link_turns_off_and_reports_it() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("bnep0")

    value.observe_link(False, "")

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == tether.LINK_LOST


def test_link_that_is_already_up_is_adopted_as_external() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.observe_link(True, "bnep0")

    snapshot = value.snapshot()
    assert snapshot["state"] == CONNECTED
    assert snapshot["external"] is True
    assert snapshot["backend"] == ""
    assert snapshot["needs_dhcp"] is False

    # Turning an adopted link off chooses a backend to find and stop it.
    value.disconnect()
    assert chooser.calls == 1
    chooser.backend.disconnects[0][0]()
    assert value.state == OFF


def test_link_events_during_connecting_wait_for_the_backend() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    value.observe_link(True, "bnep0")  # BNEP is up; NM is still running DHCP
    assert value.state == CONNECTING
    value.observe_link(False, "")
    assert value.state == CONNECTING


def test_bluez_restart_drops_the_tether_and_ignores_old_replies() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    old_success, _ = chooser.backend.connects[0]

    value.reset_after_bluez_restart()
    old_success("bnep0")

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == tether.LINK_LOST
    assert chooser.backend.cancelled >= 1


def test_stop_cancels_timers_and_backend_callbacks() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser)
    value.start()
    value.connect()
    value.stop()

    assert timers.pending == {}
    assert chooser.backend.cancelled == 1
    chooser.backend.connects[0][0]("bnep0")
    assert value.state == CONNECTING  # no transition after stop


def test_interface_names_are_bounded() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("x" * 64)
    assert value.snapshot()["interface"] == ""


def test_snapshot_never_carries_addresses_or_ip_configuration() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("bnep0")

    snapshot = value.snapshot()
    assert set(snapshot) == {
        "state", "interface", "backend", "external", "error", "needs_dhcp", "autoconnect",
    }
    assert json.loads(json.dumps(snapshot)) == snapshot


# ---- opt-in autoconnect ------------------------------------------------------


def test_autoconnect_attempts_once_ready_and_backs_off_after_refusal() -> None:
    chooser = Chooser()
    value, timers, _changes, flags = controller(chooser, autoconnect=True, ready=False)
    value.start()

    value.maybe_autoconnect()
    assert chooser.calls == 0  # MAP/PBAP still have the phone first

    flags["ready"] = True
    value.maybe_autoconnect()
    assert chooser.calls == 1
    chooser.backend.connects[0][1](tether.HOTSPOT_REFUSED)
    assert timers.delays() == [tether.AUTOCONNECT_RETRY_SECONDS]

    # A second trigger does not stack another attempt on the pending retry.
    value.maybe_autoconnect()
    assert chooser.calls == 1

    timers.fire()
    assert chooser.calls == 2
    chooser.backend.connects[1][1](tether.HOTSPOT_REFUSED)
    assert timers.delays() == [tether.AUTOCONNECT_RETRY_SECONDS * 2]


def test_autoconnect_retry_is_capped() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    delays = []
    for index in range(8):
        chooser.backend.connects[index][1](tether.HOTSPOT_REFUSED)
        delays.extend(timers.delays())
        timers.fire()
    assert delays[:3] == [30, 60, 120]
    assert delays[-1] == tether.AUTOCONNECT_RETRY_CAP_SECONDS


def test_explicit_disconnect_pauses_autoconnect_until_explicit_connect() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    chooser.backend.connects[0][0]("bnep0")

    value.disconnect()
    chooser.backend.disconnects[0][0]()
    value.maybe_autoconnect()
    value.observe_link(False, "")
    assert len(chooser.backend.connects) == 1
    assert timers.pending == {}

    value.connect()
    assert len(chooser.backend.connects) == 2


def test_autoconnect_reconnects_after_a_lost_link() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    chooser.backend.connects[0][0]("bnep0")

    value.observe_link(False, "")
    assert timers.delays() == [tether.AUTOCONNECT_RETRY_SECONDS]
    timers.fire()
    assert len(chooser.backend.connects) == 2


def test_autoconnect_waits_for_the_classic_link() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser, autoconnect=True, classic=False)
    value.start()
    value.maybe_autoconnect()
    assert chooser.calls == 0


# ---- BlueZ error mapping -----------------------------------------------------


def _error(name: str, message: str = "") -> dbus.exceptions.DBusException:
    return dbus.exceptions.DBusException(message, name=name)


@pytest.mark.parametrize(("name", "message", "token"), [
    ("org.bluez.Error.Failed", "Connection refused", tether.HOTSPOT_REFUSED),
    ("org.bluez.Error.Failed", "", tether.HOTSPOT_REFUSED),
    ("org.bluez.Error.Failed", "Host is down (112)", tether.PHONE_UNREACHABLE),
    ("org.bluez.Error.Failed", "Connection timed out", tether.TIMEOUT),
    ("org.bluez.Error.NotSupported", "", tether.NOT_SUPPORTED),
    ("org.freedesktop.DBus.Error.UnknownMethod", "", tether.NOT_SUPPORTED),
    ("org.freedesktop.DBus.Error.UnknownInterface", "", tether.NOT_SUPPORTED),
    ("org.bluez.Error.InProgress", "", tether.IN_PROGRESS),
    ("org.freedesktop.DBus.Error.NoReply", "", tether.TIMEOUT),
    ("org.freedesktop.DBus.Error.ServiceUnknown", "", tether.BLUETOOTH_UNAVAILABLE),
    ("org.freedesktop.DBus.Error.UnknownObject", "", tether.BLUETOOTH_UNAVAILABLE),
    ("org.example.Unexpected", "Joshua's iPhone", tether.GENERIC_ERROR),
])
def test_bluez_errors_map_to_public_tokens(name, message, token) -> None:
    assert bluez_error_token(_error(name, message)) == token
    assert token in tether.ERROR_TOKENS


def test_non_dbus_errors_map_to_the_generic_token() -> None:
    assert bluez_error_token(RuntimeError("boom")) == tether.GENERIC_ERROR


# ---- Network1 link watch -----------------------------------------------------


class LinkBus:
    def __init__(self, reply=None, error=None) -> None:
        self.reply = reply
        self.error = error
        self.receivers: list[tuple] = []
        self.calls: list[tuple] = []
        self.removed = 0

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply_handler, error_handler, timeout=-1.0):
        self.calls.append((bus_name, path, interface, method, args))
        if self.error is not None:
            error_handler(self.error)
        elif self.reply is not None:
            reply_handler(self.reply)

    def add_signal_receiver(self, handler, **kwargs):
        self.receivers.append((handler, kwargs))
        bus = self

        class Match:
            def remove(self) -> None:
                bus.removed += 1

        return Match()


def test_link_watch_probes_and_follows_network1_properties() -> None:
    bus = LinkBus(reply={"Connected": True, "Interface": "bnep0", "UUID": "nap"})
    seen = []
    watch = NetworkLinkWatch(lambda: bus, "/org/bluez/hci0/dev_X", lambda *a: seen.append(a))
    watch.start()

    assert seen == [(True, "bnep0")]
    handler, kwargs = bus.receivers[0]
    assert kwargs["arg0"] == "org.bluez.Network1"
    assert kwargs["path"] == "/org/bluez/hci0/dev_X"
    assert bus.calls[0][2:4] == ("org.freedesktop.DBus.Properties", "GetAll")

    handler("org.bluez.Network1", {"Connected": False}, [])
    handler("org.bluez.Device1", {"Connected": False}, [])  # other interfaces ignored
    handler("org.bluez.Network1", {"UUID": "x"}, [])
    assert seen == [(True, "bnep0"), (False, "")]

    watch.stop()
    assert bus.removed == 1


def test_link_watch_treats_a_missing_network1_as_down() -> None:
    bus = LinkBus(error=_error("org.freedesktop.DBus.Error.UnknownInterface"))
    seen = []
    NetworkLinkWatch(lambda: bus, "/dev", lambda *a: seen.append(a)).start()
    assert seen == [(False, "")]
