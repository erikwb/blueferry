"""Keep the iPhone's classic and LE Bluetooth bearers connected."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.bus import get_system_bus

log = logging.getLogger(__name__)

POLL_SECONDS = 5
# Forgive backoff only after the bearer has stayed Connected=true this long,
# so a flap cannot reset the penalty. Three poll intervals.
STABLE_CONNECTION_SECONDS = POLL_SECONDS * 3
CLASSIC_SETTLE_SECONDS = 3
# org.bluez.Bearer*.Connect uses this same timeout below. An InProgress reply
# means BlueZ owns an overlapping request, so Connected=false cannot settle it
# before a full method-timeout-sized quiet window has elapsed.
CONNECT_TIMEOUT_SECONDS = 45
# A phone that rejects a connection keeps rejecting it for a while (powered
# off, out of range, or a broken bond). Repeating Connect every poll turned
# into a five-second hammer against the iPhone; back off exponentially
# instead, up to this ceiling. Forgive the penalty only after the bearer has
# stayed connected for STABLE_CONNECTION_SECONDS.
BACKOFF_CAP_SECONDS = 300
# A live LE bearer proves that the phone is in range and answering.  Classic
# may still decline a page temporarily, but it must not inherit the five-minute
# absence backoff while ANCS is demonstrably reachable.
LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS = 30
_INTERFACES = {
    "bredr": "org.bluez.Bearer.BREDR1",
    "le": "org.bluez.Bearer.LE1",
}
# Suspected stale LE bond (report only). One observed failure mode, probably
# an LTK the iPhone no longer has after the bond was removed on one side
# only, makes the kernel/bluetoothd auto-connect loop indefinitely: the link
# comes up, encryption does not complete, and the link drops about two
# seconds later. The captured trace showed Encryption Change status 0x08
# (connection timeout), not 0x06 (PIN or key missing). On that setup,
# forgetting the computer on the iPhone, removing the device in BlueZ and
# pairing again cured it, but one case does not prove every 0x08 loop is a
# stale bond; BlueFerry therefore only reports a suspicion, re-pairing may
# help, and it never changes its own connection behaviour because of it.
#
# The pattern is deliberately narrow so ordinary radio flapping does not
# match: Classic must stay connected across the whole burst (the phone is
# demonstrably nearby), only very short links count, only Timeout, Remote
# and Authentication reasons count (Local drops come from this host, e.g.
# rfkill, adapter power or BlueFerry itself), and the drop rate must persist
# for minutes rather than for one unlucky minute.
LE_FLAP_THRESHOLD = 5
LE_FLAP_WINDOW_SECONDS = 60
# A link that drops after encryption failed lives about two seconds. Links
# that lived longer than this are ordinary drops and break a burst.
LE_FLAP_MAX_LINK_SECONDS = 5
# The burst must sustain the threshold rate, without a gap longer than the
# window, for this long before the bond is reported as suspect.
LE_FLAP_PERSIST_SECONDS = 180
# If the Bearer.LE1.Disconnected watch cannot be installed, the five-second
# polling of Bearer.LE1.Connected sees the flaps instead, sampling at most one
# transition per two polls. Use wider windows so the same threshold remains
# reachable. Without bearer interfaces (BlueZ before 5.84) LE reads unknown,
# so nothing is detected.
POLLED_LE_FLAP_WINDOW_SECONDS = 180
POLLED_LE_FLAP_PERSIST_SECONDS = 540
# Reported suspicion expires once Classic has been gone this long: the phone
# is away, and the report would otherwise say "re-pair" all day.
LE_SUSPECT_ABSENT_EXPIRY_SECONDS = 120
# A stale bond keeps the LE link cycling while the phone is near. Once no
# short drop was seen for this long, the evidence is gone and the report
# expires, also when LE simply stays down while Classic is up.
LE_SUSPECT_QUIET_EXPIRY_SECONDS = 600
_COUNTED_LE_DISCONNECT_REASONS = frozenset({"timeout", "remote", "authentication"})
# Ignore BlueZ Local-reason drops this soon after BlueFerry's own Disconnect.
OWN_LE_DISCONNECT_GRACE_SECONDS = 10
_LE_DISCONNECT_REASONS = {
    "org.bluez.Reason.Unknown": "unknown",
    "org.bluez.Reason.Timeout": "timeout",
    "org.bluez.Reason.Local": "local",
    "org.bluez.Reason.Remote": "remote",
    "org.bluez.Reason.Authentication": "authentication",
    "org.bluez.Reason.Suspend": "suspend",
}
_PUBLIC_ERROR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,255}$")


def _public_error_token(name: str, message: str) -> str:
    detail = f"{name} {message}".casefold()
    if "forbidden" in detail or "not authorized" in detail:
        return "authorization-required"
    if "connection refused" in detail or "(111)" in detail:
        return "connection-refused"
    if "timed out" in detail or "timeout" in detail:
        return "connection-timeout"
    if "cancel" in detail or "abort" in detail:
        return "connection-aborted"
    if "not supported" in detail or "unsupported" in detail:
        return "unsupported"
    return "connection-failed"

def _connect_error_parts(error: Exception) -> tuple[str, str]:
    """Split a connect failure into D-Bus name and message."""
    if isinstance(error, dbus.exceptions.DBusException):
        name = error.get_dbus_name() or type(error).__name__
        message = error.get_dbus_message() or ""
    else:
        name = type(error).__name__
        message = str(error)
    return name[:256], message[:256]


def preferred_bearer_unavailable(error: Exception) -> bool:
    """True when this BlueZ build does not expose Device1.PreferredBearer.

    Packaged bluetoothd reports that as UnknownProperty. Some experimental
    trees raise org.bluez.Error.InvalidArguments with "No such property".
    """
    if not isinstance(error, dbus.exceptions.DBusException):
        return False
    name = error.get_dbus_name() or ""
    if name in {
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownProperty",
    }:
        return True
    detail = (error.get_dbus_message() or "").casefold()
    return "no such property" in detail and "preferredbearer" in detail


def bearer_connected_unavailable(error: Exception) -> bool:
    """True when a BlueZ bearer interface has no Connected property."""
    if not isinstance(error, dbus.exceptions.DBusException):
        return False
    name = error.get_dbus_name() or ""
    if name in {
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownProperty",
    }:
        return True
    detail = (error.get_dbus_message() or "").casefold()
    return "no such property" in detail and "connected" in detail


ReadConnected = Callable[[str], bool | None]
Connect = Callable[[str, Callable[[], None], Callable[[Exception], None]], None]
Disconnect = Callable[[str, Callable[[], None], Callable[[Exception], None]], None]
Prefer = Callable[[str], None]
ObserveLeState = Callable[[bool | None], None]
ObserveLeDial = Callable[[bool], None]
InboundLePrimed = Callable[[], bool]
OnLeDisconnected = Callable[[str, str], None]
OnLeProperties = Callable[[str, dict, list], None]
# Subscribe to Bearer.LE1.Disconnected and the device's PropertiesChanged;
# return a callable that removes both subscriptions.
WatchLe = Callable[[OnLeDisconnected, OnLeProperties], Callable[[], None]]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]


def watch_bluez_le(
    device_path: str,
    on_disconnected: OnLeDisconnected,
    on_properties: OnLeProperties,
) -> Callable[[], None]:
    """Subscribe to one device's LE disconnect reasons and property changes.

    ``org.bluez.Bearer.LE1.Disconnected(s name, s message)`` has existed
    since BlueZ 5.84 (experimental interface). BlueZ emits it for every LE
    link loss with an ``org.bluez.Reason.*`` name. dbus-python follows the
    ``org.bluez`` owner, so the subscription survives bluetoothd restarts.
    """
    bus = get_system_bus()
    matches = [
        bus.add_signal_receiver(
            on_disconnected,
            dbus_interface=_INTERFACES["le"],
            signal_name="Disconnected",
            bus_name="org.bluez",
            path=device_path,
        ),
        bus.add_signal_receiver(
            on_properties,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            bus_name="org.bluez",
            path=device_path,
        ),
    ]

    def remove() -> None:
        for match in matches:
            match.remove()

    return remove


class BearerSupervisor:
    """Connect BR/EDR first, then keep LE connected alongside it.

    Pairing creates bonds, not a durable connection policy. Desktop Bluetooth
    applets often connect a newly paired device as a side effect; this class
    makes the backend independent of that desktop-specific behavior.
    """

    def __init__(
        self,
        device_path: str,
        *,
        le_enabled: bool = True,
        on_status: Callable[[], None] | None = None,
        on_le_state: ObserveLeState | None = None,
        on_le_dial: ObserveLeDial | None = None,
        read_connected: ReadConnected | None = None,
        connect: Connect | None = None,
        disconnect: Disconnect | None = None,
        prefer: Prefer | None = None,
        inbound_le_primed: InboundLePrimed | None = None,
        watch_le: WatchLe | None = None,
        le_bond_detection: Callable[[], bool] | None = None,
        on_le_bond_report: Callable[[], None] | None = None,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
        clock: Clock = time.monotonic,
    ) -> None:
        self.device_path = device_path
        self._on_status = on_status
        self._on_le_state = on_le_state
        self._on_le_dial = on_le_dial
        self._read_connected = read_connected or self._read_bluez_connected
        self._connect = connect or self._connect_bluez
        self._disconnect = disconnect or self._disconnect_bluez
        self._prefer = prefer or (
            self._prefer_bluez if connect is None else lambda _kind: None
        )
        self._inbound_le_primed = inbound_le_primed or (lambda: True)
        # Injected fakes replace BlueZ entirely; only the production
        # supervisor subscribes to the real bearer signals.
        self._watch_le = watch_le or (
            (
                lambda on_disconnected, on_properties: watch_bluez_le(
                    device_path, on_disconnected, on_properties
                )
            )
            if read_connected is None
            else None
        )
        self._unwatch_le: Callable[[], None] | None = None
        # Whether stale-bond detection applies at all, e.g. False with ANCS
        # disabled or on controllers that are not expected to finish ANCS.
        self._le_bond_detection = le_bond_detection or (lambda: True)
        # Called when the stale-bond report flips. Deliberately not
        # on_status: no bearer changed, and its listeners act on bearers.
        self._on_le_bond_report = on_le_bond_report
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._le_enabled = le_enabled
        self._timer_id: int | None = None
        self._le_settle_id: int | None = None
        self._running = False
        self._generation = 0
        self._connecting: set[str] = set()
        self._connect_request_serial = 0
        self._connect_request_ids: dict[str, int] = {}
        self._connect_targeted: dict[str, bool] = {}
        self._connect_observation_deadlines: dict[str, float] = {}
        self._disconnecting: set[str] = set()
        # A concrete False clears this hint; a transient unavailable read does
        # not. It affects only Classic targeting/backoff, while _states["le"]
        # remains the exact value published to ANCS consumers.
        self._le_targeting_hint = False
        self._le_dial_active = False
        self._le_preference_restore_pending = False
        # Outbound Bearer.LE1.Connect is only a bootstrap hint.  Real-device
        # traces show the solicitation advertisement to be the reliable
        # reconnect path, while repeating Connect can leave BlueZ in an
        # InProgress loop.  Spend one dial, then leave the radio to solicitation
        # until BlueZ is replaced or ANCS deliberately resets the transport.
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._le_reset_failures = 0
        self._next_le_reset_attempt = 0.0
        self._last_errors: dict[str, tuple[str, str]] = {}
        self._states: dict[str, bool | None] = {"bredr": None, "le": None}
        self._failures: dict[str, int] = {"bredr": 0, "le": 0}
        self._next_attempt: dict[str, float] = {"bredr": 0.0, "le": 0.0}
        self._connected_since: dict[str, float | None] = {"bredr": None, "le": None}
        # One-shot: a held LE link may forgive Classic backoff earned while the
        # phone was away, not failures that happened with ANCS already up.
        self._le_dwell_should_forgive_classic = False
        # Stale-bond detection (report only). Timestamps of short-lived LE
        # drops in the current burst; see LE_FLAP_THRESHOLD.
        self._le_flaps: deque[float] = deque()
        self._le_flap_count = 0
        self._le_burst_started_at: float | None = None
        self._bredr_lost_at: float | None = None
        self._le_bond_suspect = False
        self._le_disconnect_signal_seen = False
        self._le_link_up_at: float | None = None
        self._le_last_drop_at: float | None = None
        self._le_last_polled_drop_at: float | None = None
        self._last_le_disconnect_reason = ""
        self._own_le_disconnect_at: float | None = None

    @property
    def bredr_connected(self) -> bool:
        return self._states["bredr"] is True

    @property
    def le_connected(self) -> bool:
        return self._states["le"] is True

    @property
    def bredr_state(self) -> bool | None:
        """Return the latest observed BR/EDR bearer state."""
        return self._states["bredr"]

    @property
    def le_state(self) -> bool | None:
        """Return the latest observed LE bearer state."""
        return self._states["le"]

    @property
    def le_bond_suspect(self) -> bool:
        """True while LE keeps dropping in a way that suggests a stale bond.

        This is a report only. BlueFerry's connection behaviour does not
        depend on it.
        """
        return self._le_bond_suspect

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_le_watch()
        self._tick()
        self._timer_id = self._schedule(POLL_SECONDS, self._tick)

    def enable_le(self) -> None:
        """Allow outbound LE bootstrap after the current Classic profile attempt.

        Callers hold outbound LE work during every whole-device reconnect so
        MAP/PBAP can get the first host-initiated transaction. Phone-initiated
        LE links remain usable throughout. Calling this more than once is
        harmless.
        """
        if self._le_enabled:
            return
        self._le_enabled = True
        log.info("MAP/PBAP attempt completed; enabling outbound iPhone LE bootstrap")
        if self._states["le"] is not True:
            # Pairing deliberately leaves the untyped Device1.Connect path on
            # BR/EDR while MAP/PBAP get their first turn. Hand inbound control
            # back once here, before either an LE dial or a solicited link is
            # in flight. Bearer.LE1.Connect already names its transport and
            # must not rewrite PreferredBearer underneath itself.
            self._le_preference_restore_pending = True
            self._restore_le_preference()
        if self._running:
            self._tick()

    def hold_le(self) -> None:
        """Prevent outbound LE dialing during a MAP/PBAP attempt.

        An LE link that is already established or arrives inbound is left alone.
        If BlueFerry already has an outbound LE request in flight, ask BlueZ to
        cancel it once. If that request wins the race and establishes a link
        anyway, keep the link rather than churning a connection iOS initiated
        or accepted.
        """
        was_enabled = self._le_enabled
        self._le_enabled = False
        self._cancel_le_settle()
        if "le" in self._connecting:
            self._request_le_disconnect(force=True)
        if was_enabled:
            log.info(
                "holding outbound iPhone LE bootstrap until the MAP/PBAP "
                "attempt completes"
            )

    def poke(self) -> None:
        """Run a health check now, for example after system resume."""
        if self._running:
            self._tick()

    def reset_after_bluez_restart(self) -> None:
        """Forget callbacks and observations owned by the previous daemon."""
        previous = dict(self._states)
        self._generation += 1
        self._connecting.clear()
        self._connect_request_ids.clear()
        self._connect_targeted.clear()
        self._connect_observation_deadlines.clear()
        self._disconnecting.clear()
        self._set_le_dial_active(False)
        self._le_targeting_hint = False
        self._le_preference_restore_pending = False
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._le_reset_failures = 0
        self._next_le_reset_attempt = 0.0
        self._cancel_le_settle()
        self._states = {"bredr": None, "le": None}
        self._failures = {"bredr": 0, "le": 0}
        self._next_attempt = {"bredr": 0.0, "le": 0.0}
        self._connected_since = {"bredr": None, "le": None}
        self._le_dwell_should_forgive_classic = False
        self._last_errors.clear()
        # A new bluetoothd generation reloads its keys; judge it afresh.
        self._clear_le_flaps("BlueZ restarted", notify=False)
        self._le_disconnect_signal_seen = False
        self._le_link_up_at = None
        self._own_le_disconnect_at = None
        if self._on_le_state is not None:
            self._on_le_state(None)
        if any(value is not None for value in previous.values()) and self._on_status:
            self._on_status()
        if self._running:
            self._tick()

    @property
    def busy(self) -> bool:
        return bool(self._connecting or self._disconnecting)

    def recover_le_transport(self, *, allow_disconnected: bool = False) -> None:
        """Request one serialized LE reset after GATT and bearer state diverge."""
        if not self._running:
            return
        # A successful reset is published locally as disconnected before the
        # polling source can see a replacement inbound link. Ignore duplicate
        # failure reports during that synthetic down-state.
        if self._states["le"] is False and not allow_disconnected:
            return
        if not self._le_reset_pending:
            log.warning(
                "ANCS transport disagrees with BlueZ; resetting the LE bearer"
            )
        # The targeted disconnect creates a new transport generation, so it is
        # appropriate to give that generation one fresh outbound bootstrap.
        self._le_dial_spent = False
        self._le_reset_pending = True
        self._cancel_le_settle()
        if self._le_enabled:
            self._request_le_disconnect()
        else:
            log.debug("deferring the ANCS LE reset until the profile attempt completes")

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        self._connecting.clear()
        self._connect_request_ids.clear()
        self._connect_targeted.clear()
        self._connect_observation_deadlines.clear()
        self._disconnecting.clear()
        self._set_le_dial_active(False)
        self._le_targeting_hint = False
        self._le_preference_restore_pending = False
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._cancel_le_settle()
        self._stop_le_watch()
        if self._timer_id is not None:
            try:
                self._cancel(self._timer_id)
            except Exception:
                log.debug("could not remove bearer health timer", exc_info=True)
            self._timer_id = None

    def snapshot(self) -> dict[str, object]:
        name, message = self._last_errors.get("le", ("", ""))
        return {
            "bredr": self.bredr_connected,
            "le": self.le_connected,
            "last_le_error": (
                name if _PUBLIC_ERROR_NAME.fullmatch(name) else "connection-failed"
            ) if name else "",
            "last_le_error_message": (
                _public_error_token(name, message) if name or message else ""
            ),
            "le_bond_suspect": self._le_bond_suspect,
            "le_flap_count": self._le_flap_count,
            "last_le_disconnect_reason": self._last_le_disconnect_reason,
        }

    # ---- broken LE bond detection -----------------------------------------

    def note_le_usable(self, reason: str) -> None:
        """Record positive evidence that LE encrypted, e.g. ANCS authorized."""
        self._clear_le_flaps(reason)

    def _start_le_watch(self) -> None:
        if self._watch_le is None or self._unwatch_le is not None:
            return
        try:
            self._unwatch_le = self._watch_le(
                self._on_le_disconnected_signal,
                self._on_le_properties_changed,
            )
        except Exception:
            # Polling transitions remain the fallback detection source.
            log.debug("could not watch iPhone LE bearer signals", exc_info=True)

    def _stop_le_watch(self) -> None:
        unwatch, self._unwatch_le = self._unwatch_le, None
        if unwatch is None:
            return
        try:
            unwatch()
        except Exception:
            log.debug("could not remove iPhone LE bearer watch", exc_info=True)

    def _on_le_disconnected_signal(self, name: str, _message: str) -> None:
        """Handle org.bluez.Bearer.LE1.Disconnected(name, message)."""
        if not self._running:
            return
        first_signal = not self._le_disconnect_signal_seen
        self._le_disconnect_signal_seen = True
        reason = _LE_DISCONNECT_REASONS.get(str(name), "unknown")
        now = self._clock()
        polled = self._le_last_polled_drop_at
        if (
            first_signal
            and polled is not None
            and now - polled <= POLL_SECONDS * 2
            and self._le_flaps
            and self._le_flaps[-1] == polled
            # A link that came up after the polled drop is a later one,
            # and so is the drop this signal reports.
            and (self._le_link_up_at is None or self._le_link_up_at <= polled)
        ):
            # Polling counted this very drop before the first signal arrived;
            # the signal, with its reason and link age, replaces that count.
            self._le_flaps.pop()
            self._le_flap_count = max(0, self._le_flap_count - 1)
            if not self._le_flaps:
                # No drop is left to date the burst from; a signal that is
                # not counted below must not leave a stale start behind.
                self._le_burst_started_at = None
        self._le_last_polled_drop_at = None
        up_at, self._le_link_up_at = self._le_link_up_at, None
        lifetime = None if up_at is None else now - up_at
        if reason == "suspend":
            return
        if (
            reason == "local"
            and self._own_le_disconnect_at is not None
            and now - self._own_le_disconnect_at < OWN_LE_DISCONNECT_GRACE_SECONDS
        ):
            # BlueFerry's own reset; not even worth reporting as last reason.
            return
        self._last_le_disconnect_reason = reason
        self._record_le_drop(lifetime, reason)

    def _on_le_properties_changed(
        self,
        interface: str,
        changed: dict,
        _invalidated: list,
    ) -> None:
        if not self._running:
            return
        interface = str(interface)
        if interface == _INTERFACES["le"] and "Connected" in changed:
            if bool(changed["Connected"]):
                self._le_link_up_at = self._clock()
        if interface in {_INTERFACES["le"], "org.bluez.Device1"} and any(
            bool(changed.get(key)) for key in ("Paired", "Bonded")
        ):
            # Only a new bond resets the evidence. BlueZ 5.87 leaves a stored
            # LTK's Paired/Bonded untouched when encryption fails, and it
            # clears Paired on disconnect only for unbonded pairings. A removed
            # bond stops the daemon through its periodic bond check instead.
            self._clear_le_flaps("iPhone bond changed")

    def _record_le_drop(self, lifetime: float | None, reason: str | None) -> None:
        """Count one LE drop towards a stale-bond burst, or break the burst.

        ``reason`` is the normalized BlueZ reason, or None when only polling
        observed the drop.
        """
        if not self._le_bond_detection_active():
            self._reset_le_burst()
            return
        if lifetime is not None and lifetime >= STABLE_CONNECTION_SECONDS:
            # The link held long enough to be usable; this was an ordinary
            # departure, not a failed encryption.
            self._clear_le_flaps("LE link held")
            return
        if reason is not None and reason not in _COUNTED_LE_DISCONNECT_REASONS:
            # Local drops come from this host (rfkill, adapter power,
            # BlueFerry); unknown reasons are no evidence either way.
            return
        signal = reason is not None
        if signal and (lifetime is None or lifetime > LE_FLAP_MAX_LINK_SECONDS):
            # Signal mode measures each link from its Connected transition. A
            # link of unknown or moderate age is not the two-second pattern.
            self._reset_le_burst()
            return
        now = self._clock()
        bredr_since = self._connected_since["bredr"]
        if self._states["bredr"] is not True or bredr_since is None:
            # Without Classic the phone may be leaving; that is not evidence.
            self._reset_le_burst()
            return
        window = (
            LE_FLAP_WINDOW_SECONDS
            if self._le_disconnect_signal_seen
            else POLLED_LE_FLAP_WINDOW_SECONDS
        )
        persist = (
            LE_FLAP_PERSIST_SECONDS
            if self._le_disconnect_signal_seen
            else POLLED_LE_FLAP_PERSIST_SECONDS
        )
        if self._le_flaps and now - self._le_flaps[-1] > window:
            # A quiet gap longer than the window ends the burst.
            self._reset_le_burst()
        if self._le_burst_started_at is not None and bredr_since > self._le_burst_started_at:
            # Classic dropped during the burst; start over from this drop.
            self._reset_le_burst()
        if self._le_burst_started_at is None:
            self._le_burst_started_at = now
        burst_started = self._le_burst_started_at
        self._le_flaps.append(now)
        self._le_last_drop_at = now
        if not signal:
            self._le_last_polled_drop_at = now
        while self._le_flaps and now - self._le_flaps[0] > window:
            self._le_flaps.popleft()
        self._le_flap_count += 1
        if (
            self._le_bond_suspect
            or len(self._le_flaps) < LE_FLAP_THRESHOLD
            or now - burst_started < persist
        ):
            return
        self._le_bond_suspect = True
        log.warning(
            "iPhone LE link keeps dropping within seconds while Classic stays "
            "connected (%d short drops in the last %ds, pattern seen for %ds, "
            "last reason: %s). The LE pairing may be stale, for example after "
            "it was removed on only one side; re-pairing may help: on the "
            "iPhone open Settings > Bluetooth, tap (i) next to this computer "
            "and choose Forget This Device; on this computer run 'bluetoothctl "
            "remove <iPhone address>' and pair again.",
            len(self._le_flaps),
            window,
            int(now - burst_started),
            self._last_le_disconnect_reason or "not reported",
        )
        if self._on_le_bond_report is not None:
            self._on_le_bond_report()

    def _clear_le_flaps_if_held(self) -> None:
        """Forgive flaps once the current LE link has stayed up long enough.

        Five-second polling can see Connected=true on consecutive probes while
        the link actually cycles every two seconds in between. Once BlueZ's
        Disconnected signal is known to work, only a link age measured from
        its own transitions counts.
        """
        if not (self._le_flaps or self._le_bond_suspect):
            return
        since = (
            self._le_link_up_at
            if self._le_disconnect_signal_seen
            else self._connected_since["le"]
        )
        if since is not None and self._clock() - since >= STABLE_CONNECTION_SECONDS:
            self._clear_le_flaps("LE link held")

    def _le_bond_detection_active(self) -> bool:
        if not self._le_enabled:
            return False
        try:
            return bool(self._le_bond_detection())
        except Exception:
            log.debug("could not decide whether LE bond detection applies", exc_info=True)
            return False

    def _reset_le_burst(self) -> None:
        """Forget the current burst without touching a reported suspicion."""
        self._le_flaps.clear()
        self._le_burst_started_at = None
        if not self._le_bond_suspect:
            self._le_flap_count = 0

    def _age_le_evidence(self) -> None:
        """Let old drops decay and expire a suspicion once the phone left."""
        now = self._clock()
        if self._le_flaps and not self._le_bond_suspect:
            window = (
                LE_FLAP_WINDOW_SECONDS
                if self._le_disconnect_signal_seen
                else POLLED_LE_FLAP_WINDOW_SECONDS
            )
            if now - self._le_flaps[-1] > window:
                self._reset_le_burst()
        if (
            self._le_bond_suspect
            and self._bredr_lost_at is not None
            and now - self._bredr_lost_at >= LE_SUSPECT_ABSENT_EXPIRY_SECONDS
        ):
            self._clear_le_flaps("iPhone away")
        if (
            self._le_bond_suspect
            and self._le_last_drop_at is not None
            and now - self._le_last_drop_at >= LE_SUSPECT_QUIET_EXPIRY_SECONDS
        ):
            self._clear_le_flaps("no short LE drops for a while")
        if self._le_bond_suspect and not self._le_bond_detection_active():
            self._clear_le_flaps("detection no longer applies")

    def _clear_le_flaps(self, reason: str, *, notify: bool = True) -> None:
        self._le_flaps.clear()
        self._le_flap_count = 0
        self._le_burst_started_at = None
        if not self._le_bond_suspect:
            return
        self._le_bond_suspect = False
        log.info("iPhone LE bond no longer suspect: %s", reason)
        if notify and self._on_le_bond_report is not None:
            self._on_le_bond_report()

    def _tick(self) -> bool:
        if not self._running:
            return False

        log.debug("probing iPhone BR/EDR and LE bearer state")
        bredr = self._read("bredr")
        le = self._read("le")
        self._update_state("bredr", bredr)
        # The profile gate controls BlueFerry's outbound LE requests, not links
        # initiated by the phone. Publishing an inbound link lets ANCS perform
        # its authorization handshake while Classic recovers independently.
        self._update_state("le", le)
        self._age_le_evidence()

        if self._le_reset_pending and self._le_enabled:
            if le is False:
                self._complete_le_reset()
            elif le is True:
                self._request_le_disconnect()
                return True
            else:
                return True

        # Establish the normal Bluetooth ACL/profile connection before LE.
        # This is the order used by the proven manual setup flow and avoids
        # racing MAP/PBAP profile discovery against GATT discovery.
        if bredr is not True:
            self._cancel_le_settle()
        if bredr is False:
            self._request_connect("bredr")
        elif bredr is True and le is False and self._le_enabled:
            self._restore_le_preference()
            if "bredr" not in self._connecting:
                self._schedule_le_connect()
        elif le is True:
            self._cancel_le_settle()
        return True

    def _schedule_le_connect(self) -> None:
        if self._le_settle_id is not None or self._le_dial_exhausted():
            return
        log.info(
            "iPhone BR/EDR connected; allowing %ds to settle before LE",
            CLASSIC_SETTLE_SECONDS,
        )
        self._le_settle_id = self._schedule(
            CLASSIC_SETTLE_SECONDS,
            self._connect_le_after_settle,
        )

    def _connect_le_after_settle(self) -> bool:
        self._le_settle_id = None
        if not self._running:
            return False
        bredr = self._read("bredr")
        le = self._read("le")
        self._update_state("bredr", bredr)
        self._update_state("le", le)
        if bredr is True and le is False and self._le_enabled:
            # The handoff may have failed when LE was enabled or when this
            # timer was armed. Retry at the last safe point before the
            # targeted dial; a transient Set failure must not suppress the
            # one outbound bootstrap, and the pending flag remains set so a
            # later Classic-up/LE-down tick can try again.
            self._restore_le_preference()
            self._request_connect("le")
        elif bredr is False:
            self._request_connect("bredr")
        return False

    def _cancel_le_settle(self) -> None:
        if self._le_settle_id is None:
            return
        try:
            self._cancel(self._le_settle_id)
        except Exception:
            log.debug("could not remove LE settling timer", exc_info=True)
        self._le_settle_id = None

    def _read(self, kind: str) -> bool | None:
        try:
            return self._read_connected(kind)
        except dbus.exceptions.DBusException as error:
            log.debug(
                "%s bearer state unavailable: %s",
                kind.upper(),
                error.get_dbus_name() or str(error),
            )
            return None

    def _update_state(
        self,
        kind: str,
        value: bool | None,
        *,
        deliberate: bool = False,
    ) -> None:
        self._settle_in_progress_connect(kind, value)
        previous = self._states[kind]
        self._states[kind] = value
        if kind == "le":
            if value is True:
                self._le_targeting_hint = True
            elif value is False:
                self._le_targeting_hint = False
        if previous != value:
            label = "unknown" if value is None else ("connected" if value else "disconnected")
            log.debug("iPhone %s bearer state: %s", kind.upper(), label)
        if (
            kind == "bredr"
            and previous is False
            and value is True
            and self._states["le"] is not True
        ):
            # A Classic return is fresh evidence that the phone is reachable.
            # The one LE dial granted by the earlier disconnect may have been
            # spent while the phone was still away, so give this presence
            # generation one new bootstrap before yielding to solicitation.
            self._rearm_le_dial("iPhone BR/EDR returned")
        if kind == "le" and previous is True and value is False:
            # A real bearer lifecycle transition invalidates the previous
            # one-shot budget. This is deliberately transition-driven: a
            # device that remains absent still receives only one attempt.
            self._rearm_le_dial("iPhone LE disconnected")
            if not deliberate and not self._le_disconnect_signal_seen:
                # Until a Bearer.LE1.Disconnected signal has arrived, the
                # poll counts the drop; it samples only some flaps, hence
                # the wider window.
                since = self._connected_since["le"]
                self._record_le_drop(
                    None if since is None else self._clock() - since,
                    None,
                )
        if kind == "bredr":
            if value is True:
                self._bredr_lost_at = None
            elif previous is True:
                self._bredr_lost_at = self._clock()
                self._reset_le_burst()
        if kind == "le" and previous is not True and value is True:
            if self._le_link_up_at is None:
                self._le_link_up_at = self._clock()
        if value is True:
            if self._connected_since[kind] is None:
                self._connected_since[kind] = self._clock()
                if kind == "le":
                    self._le_dwell_should_forgive_classic = (
                        self._failures["bredr"] > 0
                        or self._clock() < self._next_attempt["bredr"]
                    )
            self._clear_backoff_if_stable(kind)
            if kind == "le":
                self._clear_le_flaps_if_held()
        elif value is False:
            self._connected_since[kind] = None
            if kind == "le":
                self._le_dwell_should_forgive_classic = False
        # Repeating stale Connected=true after a failed GATT operation can
        # race BlueZ's pending CCC registration with ATT teardown. Consumers
        # therefore receive only genuine bearer lifecycle transitions.
        if previous != value and kind == "le" and self._on_le_state is not None:
            self._on_le_state(value)
        if previous != value and self._on_status is not None:
            self._on_status()
        if kind == "le" and value is True:
            self._retarget_pending_classic_connect()

    def _classic_connect_should_be_targeted(self) -> bool:
        """Keep Classic transport-specific across transient LE read failures."""
        return self._le_targeting_hint or self._states["le"] is True

    def _retarget_pending_classic_connect(self) -> None:
        """Supersede an untyped Classic request after an inbound LE arrival."""
        if "bredr" not in self._connecting or self._connect_targeted.get("bredr"):
            return
        log.info(
            "iPhone LE connected during untyped Classic bootstrap; "
            "retargeting Classic bearer"
        )
        self._clear_connect_request("bredr")
        # The replacement request must not inherit a quiet window from the
        # obsolete untyped request.
        self._next_attempt["bredr"] = 0.0
        self._request_connect("bredr")

    def _request_connect(self, kind: str) -> None:
        if kind in self._connecting:
            return
        if kind == "bredr" and "le" in self._connecting:
            return
        if kind == "le" and "bredr" in self._connecting:
            return
        if (
            kind == "bredr"
            and self._le_enabled
            and self._states["le"] is None
            and not self._classic_connect_should_be_targeted()
        ):
            # Do not guess that LE is down and rewrite PreferredBearer during
            # a transient read failure. During the initial MAP-first gate, a
            # missing LE interface must not strand Classic-only controllers.
            return
        if kind == "le" and self._le_dial_exhausted():
            return
        if self._clock() < self._next_attempt[kind]:
            return
        if kind == "le":
            self._le_dial_spent = True
        targeted = kind == "bredr" and self._classic_connect_should_be_targeted()
        self._connecting.add(kind)
        self._connect_request_serial += 1
        request_id = self._connect_request_serial
        self._connect_request_ids[kind] = request_id
        self._connect_targeted[kind] = targeted
        generation = self._generation
        log.info("connecting iPhone %s bearer", kind.upper())
        try:
            if kind == "bredr":
                if not targeted:
                    # Device1.Connect is transport-agnostic, so select Classic
                    # only when no LE link is live. With live LE,
                    # _connect_bluez uses targeted Bearer.BREDR1.Connect and
                    # leaves the preference untouched.
                    self._prefer("bredr")
                    self._le_preference_restore_pending = True
            else:
                # Advertising while this asynchronous call is outstanding can
                # abort the dial locally. The daemon withdraws solicitation
                # synchronously here and restores it on reply or timeout.
                self._set_le_dial_active(True)
            self._connect(
                kind,
                lambda: self._connect_succeeded(kind, generation, request_id),
                lambda error: self._connect_failed(
                    kind,
                    error,
                    generation,
                    request_id,
                ),
            )
        except Exception as error:
            self._connect_failed(kind, error, generation, request_id)

    def _connect_succeeded(
        self,
        kind: str,
        generation: int,
        request_id: int,
    ) -> None:
        if not self._connect_request_is_current(kind, generation, request_id):
            return
        self._clear_connect_request(kind)
        if kind == "le":
            self._set_le_dial_active(False)
        self._last_errors.pop(kind, None)
        if kind == "bredr":
            self._restore_le_preference()
        # A successful method reply only means BlueZ accepted the request; it
        # does not mean the bearer connected. Keep widening the quiet window
        # until an observed Connected=true resets it.
        self._failures[kind] += 1
        delay = min(
            POLL_SECONDS * (2 ** self._failures[kind]),
            self._backoff_cap(kind),
        )
        self._next_attempt[kind] = self._clock() + delay
        log.info(
            "iPhone %s bearer connection requested successfully; "
            "waiting up to %ds for state change",
            kind.upper(),
            delay,
        )

    def _connect_failed(
        self,
        kind: str,
        error: Exception,
        generation: int,
        request_id: int,
    ) -> None:
        if not self._connect_request_is_current(kind, generation, request_id):
            return
        name, message = _connect_error_parts(error)
        if name == "org.bluez.Error.InProgress":
            # BlueZ still owns a connect attempt. Keep solicitation withdrawn
            # and keep this bearer in _connecting until Connected=true or a
            # full Connect-sized timeout. Connected=false is normal while the
            # overlapping operation is still running and proves nothing.
            self._connect_observation_deadlines.setdefault(
                kind,
                self._clock() + CONNECT_TIMEOUT_SECONDS,
            )
            log.debug("%s bearer connection is still in progress", kind.upper())
            return
        if name == "org.bluez.Error.AlreadyConnected":
            if kind == "bredr" and not self._connect_targeted.get(kind, False):
                bredr = self._read("bredr")
                self._update_state("bredr", bredr)
                if bredr is not True:
                    # Device1.Connected can mean only LE is up. Refresh that
                    # state so an inbound link supersedes this untyped request
                    # with Bearer.BREDR1.Connect instead of creating a false
                    # Classic success and quiet window.
                    self._update_state("le", self._read("le"))
                    if not self._connect_request_is_current(
                        kind,
                        generation,
                        request_id,
                    ):
                        return
                else:
                    log.debug("%s bearer connection already active", kind.upper())
                    self._connect_succeeded(kind, generation, request_id)
                    return
            else:
                log.debug("%s bearer connection already active", kind.upper())
                self._connect_succeeded(kind, generation, request_id)
                return
        self._clear_connect_request(kind)
        if kind == "le":
            self._set_le_dial_active(False)
        if kind == "le" and not self._le_enabled:
            # Policy closed the gate while the asynchronous request was in
            # flight.  This generation never received its permitted attempt.
            self._le_dial_spent = False
            return
        self._failures[kind] += 1
        delay = min(
            POLL_SECONDS * (2 ** self._failures[kind]),
            self._backoff_cap(kind),
        )
        self._next_attempt[kind] = self._clock() + delay
        if self._last_errors.get(kind) != (name, message):
            detail = f"{name}: {message}" if message else name
            log.warning(
                "could not connect iPhone %s bearer: %s (next attempt in %ds)",
                kind.upper(),
                detail,
                delay,
            )
            self._last_errors[kind] = (name, message)

    def _connect_request_is_current(
        self,
        kind: str,
        generation: int,
        request_id: int,
    ) -> bool:
        return (
            generation == self._generation
            and self._connect_request_ids.get(kind) == request_id
        )

    def _clear_connect_request(self, kind: str) -> None:
        self._connecting.discard(kind)
        self._connect_request_ids.pop(kind, None)
        self._connect_targeted.pop(kind, None)
        self._connect_observation_deadlines.pop(kind, None)

    def _settle_in_progress_connect(self, kind: str, value: bool | None) -> None:
        """Resolve InProgress on connection or after a method-sized timeout."""
        deadline = self._connect_observation_deadlines.get(kind)
        if deadline is None:
            return
        connected = value is True
        if not connected and self._clock() < deadline:
            return
        self._clear_connect_request(kind)
        if kind == "le":
            self._set_le_dial_active(False)
        if connected:
            log.debug("%s bearer InProgress state connected", kind.upper())
            return

        # The operation remained down for a full BlueZ Connect timeout. Count
        # that as a failure so Classic observes normal exponential backoff.
        # LE deliberately keeps its one-shot spent and yields to solicitation.
        self._failures[kind] += 1
        delay = min(
            POLL_SECONDS * (2 ** self._failures[kind]),
            self._backoff_cap(kind),
        )
        self._next_attempt[kind] = self._clock() + delay
        log.debug(
            "%s bearer InProgress state timed out; next attempt in %ds",
            kind.upper(),
            delay,
        )

    def _clear_backoff_if_stable(self, kind: str) -> None:
        """Forgive backoff after the bearer has stayed Connected=true long enough."""
        since = self._connected_since[kind]
        if since is None:
            return
        if self._clock() - since < STABLE_CONNECTION_SECONDS:
            return
        self._last_errors.pop(kind, None)
        self._failures[kind] = 0
        self._next_attempt[kind] = 0.0
        if kind == "le" and self._le_dwell_should_forgive_classic:
            self._reset_classic_backoff_from_le()
            self._le_dwell_should_forgive_classic = False

    def _reset_classic_backoff_from_le(self) -> None:
        """Forgive Classic absence backoff once LE has held long enough."""
        self._last_errors.pop("bredr", None)
        self._failures["bredr"] = 0
        self._next_attempt["bredr"] = 0.0

    def _restore_le_preference(self) -> None:
        """Hand untyped connects back to inbound LE once Classic is settled."""
        if (
            not self._le_preference_restore_pending
            or not self._le_enabled
            or self._classic_connect_should_be_targeted()
            or self._connecting
        ):
            return
        try:
            self._prefer("le")
        except Exception as error:
            log.warning(
                "could not hand PreferredBearer to inbound LE: %s",
                error,
            )
            return
        self._le_preference_restore_pending = False

    def _le_dial_exhausted(self) -> bool:
        """Yield to inbound solicitation only when it is actually on air."""
        return self._le_dial_spent and self._inbound_le_primed()

    def _set_le_dial_active(self, active: bool) -> None:
        """Publish one LE dial's lifetime so solicitation cannot overlap it."""
        if active == self._le_dial_active:
            return
        self._le_dial_active = active
        if self._on_le_dial is not None:
            self._on_le_dial(active)

    def _rearm_le_dial(self, reason: str) -> None:
        """Grant one outbound LE bootstrap for a new presence generation."""
        was_spent = self._le_dial_spent
        self._le_dial_spent = False
        self._failures["le"] = 0
        self._next_attempt["le"] = 0.0
        if was_spent:
            log.info("re-arming outbound iPhone LE bootstrap: %s", reason)

    def _backoff_cap(self, kind: str) -> int:
        if kind == "bredr" and self._classic_connect_should_be_targeted():
            return LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS
        return BACKOFF_CAP_SECONDS

    def _request_le_disconnect(self, *, force: bool = False) -> None:
        if "le" in self._disconnecting:
            return
        if not force and self._clock() < self._next_le_reset_attempt:
            return
        self._disconnecting.add("le")
        self._own_le_disconnect_at = self._clock()
        generation = self._generation
        try:
            self._disconnect(
                "le",
                lambda: self._disconnect_succeeded(generation),
                lambda error: self._disconnect_failed(error, generation),
            )
        except Exception as error:
            self._disconnect_failed(error, generation)

    def _disconnect_succeeded(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("le")
        if not self._le_reset_pending:
            return
        # Disconnect success is the generation boundary because iOS may
        # already be back before the next poll.
        log.info("iPhone LE bearer reset completed")
        self._complete_le_reset()

    def _complete_le_reset(self) -> None:
        """Publish a completed LE reset even if its down-state was transient."""
        self._le_reset_pending = False
        self._le_reset_failures = 0
        self._next_le_reset_attempt = 0.0
        self._update_state("le", False, deliberate=True)
        if self.bredr_connected and self._le_enabled:
            self._schedule_le_connect()

    def _disconnect_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("le")
        name, message = _connect_error_parts(error)
        if name in {
            "org.bluez.Error.NotConnected",
            "org.bluez.Error.AlreadyDisconnected",
        } or "not connected" in message.casefold():
            self._complete_le_reset()
            return
        self._le_reset_failures += 1
        delay = min(
            POLL_SECONDS * (2 ** self._le_reset_failures),
            BACKOFF_CAP_SECONDS,
        )
        self._next_le_reset_attempt = self._clock() + delay
        detail = f"{name}: {message}" if message else name
        log.warning(
            "could not reset iPhone LE bearer: %s (next attempt in %ds)",
            detail,
            delay,
        )

    def _read_bluez_connected(self, kind: str) -> bool | None:
        obj = get_system_bus().get_object("org.bluez", self.device_path)
        properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        if kind == "bredr":
            # Device1.Connected is true when *either* bearer is up. Prefer the
            # bearer-specific property so an LE-only link is not mistaken for
            # the Classic connection required by MAP/PBAP. Bearer.BREDR1 is
            # marker-only on some older packaged BlueZ builds, where the
            # aggregate property remains the only available fallback.
            try:
                return bool(
                    properties.Get(
                        _INTERFACES["bredr"],
                        "Connected",
                        timeout=5.0,
                    )
                )
            except dbus.exceptions.DBusException as error:
                if not bearer_connected_unavailable(error):
                    raise
                return bool(
                    properties.Get(
                        "org.bluez.Device1",
                        "Connected",
                        timeout=5.0,
                    )
                )
        return bool(properties.Get(_INTERFACES[kind], "Connected", timeout=5.0))

    def _connect_bluez(
        self,
        kind: str,
        on_success: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        # With no live link, Device1.Connect is the broadly compatible Classic
        # bootstrap (Bearer.BREDR1 can be marker-only). Under live LE, using
        # the targeted Classic method avoids rewriting PreferredBearer and
        # disturbing ANCS. If that method is marker-only, profile reconnects
        # still establish their own OBEX transports.
        if kind == "bredr":
            interface = (
                _INTERFACES["bredr"]
                if self._connect_targeted.get(
                    "bredr",
                    self._classic_connect_should_be_targeted(),
                )
                else "org.bluez.Device1"
            )
        else:
            interface = _INTERFACES[kind]
        bearer = dbus.Interface(
            get_system_bus().get_object("org.bluez", self.device_path),
            interface,
        )
        bearer.Connect(
            reply_handler=on_success,
            error_handler=on_error,
            timeout=float(CONNECT_TIMEOUT_SECONDS),
        )

    def _disconnect_bluez(
        self,
        kind: str,
        on_success: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        bearer = dbus.Interface(
            get_system_bus().get_object("org.bluez", self.device_path),
            _INTERFACES[kind],
        )
        bearer.Disconnect(
            reply_handler=on_success,
            error_handler=on_error,
            timeout=45.0,
        )

    def _prefer_bluez(self, kind: str) -> None:
        properties = dbus.Interface(
            get_system_bus().get_object("org.bluez", self.device_path),
            "org.freedesktop.DBus.Properties",
        )
        try:
            current = str(
                properties.Get(
                    "org.bluez.Device1",
                    "PreferredBearer",
                    timeout=5.0,
                )
            )
        except dbus.exceptions.DBusException as error:
            if preferred_bearer_unavailable(error):
                # Older or partial experimental BlueZ builds can still accept
                # inbound ANCS without exposing PreferredBearer.
                log.debug("PreferredBearer is unavailable on this BlueZ build")
                return
            current = ""
        else:
            if current == kind:
                return
        try:
            properties.Set(
                "org.bluez.Device1",
                "PreferredBearer",
                dbus.String(kind),
                timeout=5.0,
            )
            log.debug("set iPhone PreferredBearer=%s", kind)
        except dbus.exceptions.DBusException as error:
            if preferred_bearer_unavailable(error):
                log.debug("PreferredBearer is unavailable on this BlueZ build")
                return
            raise
