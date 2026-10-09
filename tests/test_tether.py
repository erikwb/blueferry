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

    def connect(self, on_connected, on_error, on_lost) -> None:
        self.connects.append((on_connected, on_error, on_lost))

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


class Link:
    """Recording stand-in for NetworkLinkWatch."""

    def __init__(self) -> None:
        self.probes = 0
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    def probe(self) -> None:
        self.probes += 1


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def controller(chooser=None, *, classic=True, ready=True, autoconnect=False, link=None,
               clock=None, interfaces=frozenset({"bnep0"}), enabled=True):
    timers = Timers()
    changes: list[None] = []
    flags = {"classic": classic, "ready": ready}
    value = TetherController(
        chooser or Chooser(),
        link_watch=link,
        classic_ready=lambda: flags["classic"],
        autoconnect_ready=lambda: flags["ready"],
        on_changed=lambda: changes.append(None),
        enabled=enabled,
        autoconnect=autoconnect,
        schedule=timers.schedule,
        cancel=timers.cancel,
        clock=clock or Clock(),
        interface_exists=lambda name: name in interfaces,
    )
    return value, timers, changes, flags


def test_default_state_is_off_and_never_connects_by_itself() -> None:
    chooser = Chooser()
    value, timers, changes, _ = controller(chooser)
    value.start()
    value.maybe_autoconnect()

    assert value.snapshot() == {
        "state": OFF, "interface": "", "backend": "", "external": False,
        "error": "", "needs_dhcp": False, "enabled": True, "autoconnect": False,
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

    on_connected, *_ = chooser.backend.connects[0]
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
    late_success, *_ = chooser.backend.connects[0]
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
    old_success, *_ = chooser.backend.connects[0]

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
        "state", "interface", "backend", "external", "error", "needs_dhcp", "enabled",
        "autoconnect",
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


# ---- adopted links, deliberate deactivation, recovery gating ----------------


def test_stopping_an_adopted_link_waits_for_bluez_to_confirm() -> None:
    link = Link()
    chooser = Chooser()
    value, *_ = controller(chooser, link=link)
    value.start()
    value.observe_link(True, "bnep0")

    value.disconnect()
    chooser.backend.disconnects[0][0]()  # the backend reports "done"

    assert value.state == DISCONNECTING
    assert link.probes == 1
    value.observe_link(False, "")
    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == ""


def test_adopted_link_that_stays_up_is_reported_not_hidden() -> None:
    link = Link()
    chooser = Chooser()
    value, timers, *_ = controller(chooser, link=link)
    value.start()
    value.observe_link(True, "bnep0")
    value.disconnect()
    chooser.backend.disconnects[0][0]()

    value.observe_link(True, "bnep0")  # still up right after the reply
    assert value.state == DISCONNECTING
    assert timers.delays() == [tether.LINK_DOWN_GRACE_SECONDS]
    timers.fire()
    assert link.probes == 2
    value.observe_link(True, "bnep0")  # still up after the grace period

    snapshot = value.snapshot()
    assert snapshot["state"] == CONNECTED
    assert snapshot["error"] == tether.GENERIC_ERROR
    assert snapshot["external"] is True


def test_external_links_are_never_chased_by_autoconnect() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.observe_link(True, "bnep0")

    value.observe_link(False, "")

    assert value.snapshot()["error"] == tether.LINK_LOST
    assert timers.pending == {}
    assert chooser.calls == 0


def test_user_deactivation_elsewhere_is_respected_like_disconnect() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    chooser.backend.connects[0][0]("bnep0")
    lost = chooser.backend.connects[0][2]

    # BlueZ often reports the link drop before NetworkManager's reason.
    value.observe_link(False, "")
    assert timers.pending  # a retry was provisionally scheduled
    lost(tether.LINK_LOST, True)

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == ""
    assert timers.pending == {}
    value.maybe_autoconnect()
    assert len(chooser.backend.connects) == 1


def test_backend_reported_loss_turns_off_and_retries_when_opted_in() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    chooser.backend.connects[0][0]("bnep0")

    chooser.backend.connects[0][2](tether.IP_CONFIG_FAILED, False)

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == tether.IP_CONFIG_FAILED
    assert timers.delays() == [tether.AUTOCONNECT_RETRY_SECONDS]


def test_link_alive_requires_an_existing_interface() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser, interfaces=frozenset())
    assert value.link_alive() is False
    value.connect()
    assert value.active is True
    assert value.link_alive() is False  # connecting never blocks recovery
    chooser.backend.connects[0][0]("bnep0")
    assert value.link_alive() is False  # bnep0 is gone from the kernel


def test_link_alive_with_a_present_interface() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][0]("bnep0")
    assert value.link_alive() is True


def test_unknown_interface_holds_recovery_for_a_bounded_time() -> None:
    clock = Clock()
    chooser = Chooser()
    value, *_ = controller(chooser, clock=clock)
    value.connect()
    chooser.backend.connects[0][0]("")

    assert value.link_alive() is True
    clock.now += tether.UNKNOWN_INTERFACE_TRUST_SECONDS
    assert value.link_alive() is False


def test_probe_link_only_while_connected() -> None:
    link = Link()
    chooser = Chooser()
    value, *_ = controller(chooser, link=link)
    value.start()
    value.probe_link()
    assert link.probes == 0
    value.connect()
    chooser.backend.connects[0][0]("bnep0")
    value.probe_link()
    assert link.probes == 1


def test_bluez_restart_clears_a_stale_failure() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser)
    value.connect()
    chooser.backend.connects[0][1](tether.HOTSPOT_REFUSED)

    value.reset_after_bluez_restart()

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == ""


# ---- the user's opt-in (off by default) -------------------------------------


def test_controller_is_disabled_unless_told_otherwise() -> None:
    value = TetherController(
        Chooser(), schedule=lambda *_a: 1, cancel=lambda _s: None,
    )
    assert value.enabled is False
    assert value.snapshot()["enabled"] is False


def test_disabled_controller_neither_watches_nor_adopts_nor_holds_recovery() -> None:
    link = Link()
    chooser = Chooser()
    value, _timers, changes, _flags = controller(chooser, link=link, enabled=False)
    value.start()

    assert link.starts == 0
    # A PAN link another tool (e.g. plasma-nm) brought up is not adopted.
    value.observe_link(True, "bnep0")
    assert value.state == OFF
    assert value.snapshot()["external"] is False
    assert value.link_alive() is False
    value.probe_link()
    assert link.probes == 0
    assert changes == []
    assert chooser.calls == 0


def test_disabled_controller_refuses_connect_with_a_clear_error() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser, enabled=False)
    value.start()

    with pytest.raises(tether.TetherDisabledError, match="turned off") as caught:
        value.connect()
    # It travels as the stable NotReady D-Bus error.
    assert isinstance(caught.value, NotReadyError)
    assert chooser.calls == 0
    assert value.state == OFF


def test_disabled_controller_never_autoconnects() -> None:
    chooser = Chooser()
    value, timers, *_ = controller(chooser, enabled=False, autoconnect=True)
    value.start()
    value.maybe_autoconnect()
    assert chooser.calls == 0
    assert timers.pending == {}


def test_disconnect_while_disabled_is_a_harmless_no_op() -> None:
    chooser = Chooser()
    value, *_ = controller(chooser, enabled=False)
    value.start()
    assert value.disconnect()["state"] == OFF
    assert chooser.calls == 0


def test_enabling_at_runtime_starts_the_watch_and_allows_connect() -> None:
    link = Link()
    value, _timers, changes, _flags = controller(link=link, enabled=False)
    value.start()

    snapshot = value.configure(True, False)

    assert snapshot["enabled"] is True
    assert link.starts == 1
    assert changes  # clients are told to refetch
    value.observe_link(True, "bnep0")
    assert value.state == CONNECTED  # adoption works once enabled
    value.configure(True, False)
    assert link.starts == 1  # idempotent


def test_enabling_before_start_waits_for_start() -> None:
    link = Link()
    value, *_ = controller(link=link, enabled=False)
    value.configure(True, False)
    assert link.starts == 0
    value.start()
    assert link.starts == 1


def test_disabling_stops_our_own_link_and_releases_recovery() -> None:
    link = Link()
    backend = Backend()
    value, *_ = controller(Chooser(backend), link=link)
    value.start()
    value.connect()
    backend.connects[0][0]("bnep0")
    assert value.link_alive() is True

    snapshot = value.configure(False, False)

    assert snapshot["state"] == DISCONNECTING
    assert snapshot["enabled"] is False
    assert len(backend.disconnects) == 1
    assert link.stops == 1
    assert value.link_alive() is False
    backend.disconnects[0][0]()
    assert value.state == OFF
    with pytest.raises(tether.TetherDisabledError):
        value.connect()


def test_disabling_while_connecting_withdraws_the_attempt() -> None:
    backend = Backend()
    value, timers, *_ = controller(Chooser(backend), link=Link())
    value.start()
    value.connect()
    assert value.state == CONNECTING

    value.configure(False, False)

    assert value.state == DISCONNECTING
    assert len(backend.disconnects) == 1
    # A late success of the withdrawn attempt cannot resurrect it.
    backend.connects[0][0]("bnep0")
    assert value.state == DISCONNECTING
    backend.disconnects[0][0]()
    assert value.state == OFF
    assert all(seconds != tether.CONNECT_DEADLINE_SECONDS for seconds in timers.delays())


def test_disabling_forgets_an_adopted_link_without_stopping_it() -> None:
    link = Link()
    chooser = Chooser()
    value, *_ = controller(chooser, link=link)
    value.start()
    value.observe_link(True, "bnep0")
    assert value.snapshot()["external"] is True

    snapshot = value.configure(False, False)

    # plasma-nm (or whoever) owns that link; BlueFerry only lets go of it.
    assert chooser.calls == 0
    assert chooser.backend.disconnects == []
    assert snapshot["state"] == OFF
    assert snapshot["external"] is False
    assert link.stops == 1
    assert value.link_alive() is False
    # Later reports from BlueZ are ignored while disabled.
    value.observe_link(True, "bnep0")
    assert value.state == OFF


def test_disabling_settles_a_stop_that_waits_for_bluez() -> None:
    link = Link()
    chooser = Chooser()
    value, *_ = controller(chooser, link=link)
    value.start()
    value.observe_link(True, "bnep0")
    value.disconnect()
    chooser.backend.disconnects[0][0]()  # NetworkManager answered; BlueZ pending
    assert value.state == DISCONNECTING

    value.configure(False, False)

    assert value.state == OFF
    assert link.stops == 1


def test_disabling_cancels_a_pending_automatic_retry() -> None:
    chooser = Chooser(fail=tether.HOTSPOT_REFUSED)
    value, timers, *_ = controller(chooser, autoconnect=True, link=Link())
    value.start()
    value.maybe_autoconnect()
    assert value.state == FAILED
    assert timers.pending

    value.configure(False, True)

    assert timers.pending == {}
    assert value.state == OFF
    value.maybe_autoconnect()
    assert chooser.calls == 1


def test_turning_on_automatic_tethering_tries_once_when_ready() -> None:
    backend = Backend()
    value, *_ = controller(Chooser(backend), link=Link())
    value.start()
    value.disconnect()  # an earlier explicit "off" pauses automatic attempts

    snapshot = value.configure(True, True)

    assert snapshot["autoconnect"] is True
    assert len(backend.connects) == 1


def test_enabling_with_automatic_tethering_waits_for_readiness() -> None:
    backend = Backend()
    value, _timers, _changes, flags = controller(
        Chooser(backend), link=Link(), enabled=False, ready=False,
    )
    value.start()
    value.configure(True, True)
    assert backend.connects == []
    flags["ready"] = True
    value.maybe_autoconnect()
    assert len(backend.connects) == 1


def test_bluez_restart_while_disabled_does_not_probe() -> None:
    link = Link()
    value, *_ = controller(link=link, enabled=False)
    value.start()
    value.reset_after_bluez_restart()
    assert link.probes == 0
    assert value.state == OFF


# ---- persisted opt-in --------------------------------------------------------


def test_settings_default_off_and_persist(isolated_state) -> None:
    from blueferry import config

    settings = tether.TetherSettings(default_enabled=False, default_autoconnect=False)
    assert (settings.enabled, settings.autoconnect) == (False, False)

    assert settings.set(True, True) == (True, True)
    reloaded = tether.TetherSettings(default_enabled=False, default_autoconnect=False)
    assert (reloaded.enabled, reloaded.autoconnect) == (True, True)
    assert json.loads(config.SETTINGS_JSON.read_text())["tether_enabled"] is True
    assert config.SETTINGS_JSON.stat().st_mode & 0o777 == 0o600


def test_settings_keep_unrelated_preferences(isolated_state) -> None:
    from blueferry import config
    from blueferry.settings_store import SettingsStore

    SettingsStore().update(proximity_lock_enabled=True)
    tether.TetherSettings(default_enabled=False).set(True, False)
    payload = json.loads(config.SETTINGS_JSON.read_text())
    assert payload["proximity_lock_enabled"] is True
    assert payload["tether_enabled"] is True


def test_settings_environment_seeds_until_a_value_is_saved(isolated_state) -> None:
    settings = tether.TetherSettings(default_enabled=True, default_autoconnect=True)
    assert (settings.enabled, settings.autoconnect) == (True, True)

    settings.set(False, False)
    reloaded = tether.TetherSettings(default_enabled=True, default_autoconnect=True)
    assert (reloaded.enabled, reloaded.autoconnect) == (False, False)


def test_saved_preference_overriding_the_environment_is_logged(
    isolated_state, monkeypatch, caplog,
) -> None:
    from blueferry import config

    tether.TetherSettings(default_enabled=False).set(False, False)
    monkeypatch.setenv("BLUEFERRY_TETHER_ENABLED", "true")
    monkeypatch.setenv("BLUEFERRY_TETHER_AUTOCONNECT", "false")
    monkeypatch.setattr(config, "TETHER_ENABLED", True)
    monkeypatch.setattr(config, "TETHER_AUTOCONNECT", False)
    with caplog.at_level("INFO", logger="blueferry.tether"):
        settings = tether.TetherSettings()
    assert (settings.enabled, settings.autoconnect) == (False, False)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "BLUEFERRY_TETHER_ENABLED is ignored" in messages[0]


@pytest.mark.parametrize(("enabled", "autoconnect"), [
    ("yes", False), (True, 1), (None, False),
])
def test_settings_reject_non_booleans(isolated_state, enabled, autoconnect) -> None:
    settings = tether.TetherSettings(default_enabled=False)
    with pytest.raises(ValueError):
        settings.set(enabled, autoconnect)
    assert settings.enabled is False


def test_enabling_with_autoconnect_waits_for_the_first_link_report() -> None:
    """A link the applet already holds must be adopted, not claimed as ours."""
    link = Link()
    backend = Backend()
    value, *_ = controller(Chooser(backend), link=link, enabled=False)
    value.start()

    value.configure(True, True)
    assert backend.connects == []  # the watch's probe has not answered yet

    value.observe_link(True, "bnep0")
    assert backend.connects == []
    assert value.snapshot()["external"] is True
    # Disabling now leaves that link alone.
    value.configure(False, True)
    assert backend.disconnects == []


def test_enabling_with_autoconnect_connects_once_the_link_is_known_down() -> None:
    backend = Backend()
    value, *_ = controller(Chooser(backend), link=Link(), enabled=False)
    value.start()
    value.configure(True, True)

    value.observe_link(False, "")
    value.observe_link(False, "")

    assert len(backend.connects) == 1


def test_a_failed_stop_after_disabling_leaves_no_stale_failure() -> None:
    backend = Backend()
    value, *_ = controller(Chooser(backend), link=Link())
    value.start()
    value.connect()
    backend.connects[0][0]("bnep0")
    value.configure(False, False)

    backend.disconnects[0][1](tether.GENERIC_ERROR)

    assert value.snapshot()["state"] == OFF
    assert value.snapshot()["error"] == ""
    value.configure(True, False)
    assert value.snapshot()["state"] == OFF


@pytest.mark.parametrize('error_name', [
    'org.freedesktop.DBus.Error.NoReply',
    'org.freedesktop.DBus.Error.Timeout',
    'org.freedesktop.DBus.Error.AccessDenied',
    'org.freedesktop.DBus.Error.Disconnected',
])
def test_failed_link_read_preserves_live_recovery_hold(error_name) -> None:
    bus = LinkBus(reply={'Connected': True, 'Interface': 'bnep0'})
    interfaces = {'bnep0'}
    value, *_ = controller(interfaces=interfaces)
    value._link_watch = NetworkLinkWatch(lambda: bus, '/dev', value.observe_link)
    value.start()
    assert value.link_alive()

    bus.error = _error(error_name)
    value.probe_link()

    assert value.state == CONNECTED
    assert value.link_alive()
    # Retaining the last report never overrides evidence from the kernel.
    interfaces.clear()
    assert not value.link_alive()
    bus.error = None
    bus.reply = {'Connected': False}
    value.probe_link()
    assert value.state == OFF


def test_unknown_stop_confirmation_keeps_adopted_link_retryable() -> None:
    bus = LinkBus(reply={'Connected': True, 'Interface': 'bnep0'})
    chooser = Chooser()
    value, timers, *_ = controller(chooser)
    value._link_watch = NetworkLinkWatch(lambda: bus, '/dev', value.observe_link)
    value.start()
    value.disconnect()
    bus.error = _error('org.freedesktop.DBus.Error.NoReply')
    chooser.backend.disconnects[0][0]()

    assert value.state == CONNECTED
    assert value.snapshot()['error'] == tether.GENERIC_ERROR
    assert value.link_alive()
    assert timers.delays() == []
    value.disconnect()
    assert len(chooser.backend.disconnects) == 2


@pytest.mark.parametrize("confirmed_up", [True, None])
def test_failed_adopted_disconnect_explains_retry_to_user(confirmed_up):
    from blueferry.tether_status import TetherStatus

    chooser = Chooser()
    value, timers, _, _ = controller(chooser, link=Link())
    value.start()
    value.observe_link(True, "bnep0")
    value.disconnect()
    chooser.backend.disconnects[-1][0]()
    value.observe_link(confirmed_up, "bnep0")
    if confirmed_up:
        timers.fire(lambda seconds: seconds == tether.LINK_DOWN_GRACE_SECONDS)
        value.observe_link(True, "bnep0")
    status = TetherStatus.from_dict(value.snapshot())
    assert status.state == CONNECTED
    assert status.error
    assert "try disconnecting again" in status.summary()
    assert "may still be active" in status.summary()
    value.stop()
