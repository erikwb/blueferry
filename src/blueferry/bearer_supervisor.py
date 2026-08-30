"""Keep the iPhone's classic and LE Bluetooth bearers connected."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.bus import get_system_bus

log = logging.getLogger(__name__)

POLL_SECONDS = 5
CLASSIC_SETTLE_SECONDS = 3
# A phone that rejects a connection keeps rejecting it for a while (powered
# off, out of range, or a broken bond). Repeating Connect every poll turned
# into a five-second hammer against the iPhone; back off exponentially
# instead, up to this ceiling, and reset as soon as a bearer connects.
BACKOFF_CAP_SECONDS = 300
# A live LE bearer proves that the phone is in range and answering.  Classic
# may still decline a page temporarily, but it must not inherit the five-minute
# absence backoff while ANCS is demonstrably reachable.
LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS = 30
CLASSIC_RECOVERY_COOLDOWN_SECONDS = 30
CLASSIC_RECOVERY_DISCONNECT_ATTEMPTS = 3
_INTERFACES = {
    "bredr": "org.bluez.Bearer.BREDR1",
    "le": "org.bluez.Bearer.LE1",
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
InboundLePrimed = Callable[[], bool]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]
Clock = Callable[[], float]


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
        read_connected: ReadConnected | None = None,
        connect: Connect | None = None,
        disconnect: Disconnect | None = None,
        prefer: Prefer | None = None,
        inbound_le_primed: InboundLePrimed | None = None,
        schedule: Schedule = GLib.timeout_add_seconds,
        cancel: Cancel = GLib.source_remove,
        clock: Clock = time.monotonic,
    ) -> None:
        self.device_path = device_path
        self._on_status = on_status
        self._on_le_state = on_le_state
        self._read_connected = read_connected or self._read_bluez_connected
        self._connect = connect or self._connect_bluez
        self._disconnect = disconnect or self._disconnect_bluez
        self._prefer = prefer or (
            self._prefer_bluez if connect is None else lambda _kind: None
        )
        self._inbound_le_primed = inbound_le_primed or (lambda: True)
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._le_enabled = le_enabled
        self._timer_id: int | None = None
        self._le_settle_id: int | None = None
        self._running = False
        self._generation = 0
        self._connecting: set[str] = set()
        self._disconnecting: set[str] = set()
        self._le_hold_disconnect_pending = False
        # Outbound Bearer.LE1.Connect is only a bootstrap hint.  Real-device
        # traces show the solicitation advertisement to be the reliable
        # reconnect path, while repeating Connect can leave BlueZ in an
        # InProgress loop.  Spend one dial, then leave the radio to solicitation
        # until BlueZ is replaced or ANCS deliberately resets the transport.
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._le_reset_failures = 0
        self._next_le_reset_attempt = 0.0
        self._classic_recovery_pending = False
        self._classic_recovery_saw_disconnect = False
        self._classic_disconnect_attempts = 0
        self._next_classic_disconnect_attempt = 0.0
        self._classic_recovery_cycles = 0
        self._next_classic_recovery_allowed = 0.0
        self._last_errors: dict[str, tuple[str, str]] = {}
        self._states: dict[str, bool | None] = {"bredr": None, "le": None}
        self._failures: dict[str, int] = {"bredr": 0, "le": 0}
        self._next_attempt: dict[str, float] = {"bredr": 0.0, "le": 0.0}

    @property
    def bredr_connected(self) -> bool:
        return self._states["bredr"] is True

    @property
    def bredr_ready(self) -> bool:
        """True when Classic is connected and no targeted reset is active."""
        return self.bredr_connected and not self._classic_recovery_pending

    @property
    def le_connected(self) -> bool:
        return self._states["le"] is True

    @property
    def le_state(self) -> bool | None:
        """Return the latest observed LE bearer state."""
        return self._states["le"]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()
        self._timer_id = self._schedule(POLL_SECONDS, self._tick)

    def enable_le(self) -> None:
        """Allow the LE bearer after the current Classic profile attempt.

        Callers hold LE during every whole-device reconnect so MAP/PBAP can be
        the first profile transaction. Calling this more than once is harmless.
        """
        if self._le_enabled:
            return
        self._le_enabled = True
        self._le_hold_disconnect_pending = False
        log.info("MAP/PBAP attempt completed; enabling iPhone LE bearer")
        if self._running:
            self._tick()

    def hold_le(self) -> None:
        """Prevent a missing LE bearer from connecting during a MAP/PBAP attempt.

        An LE link that is already established is left alone. If LE is missing,
        the gate also rejects a Connect that was already in flight, avoiding a
        race with the MAP/PBAP attempt without churning a healthy LE link.
        """
        was_enabled = self._le_enabled
        self._le_enabled = False
        if self._states["le"] is False:
            self._le_hold_disconnect_pending = True
        self._cancel_le_settle()
        if "le" in self._connecting:
            self._request_le_disconnect(force=True)
        if was_enabled:
            log.info("holding iPhone LE bearer until the MAP/PBAP attempt completes")

    def poke(self) -> None:
        """Run a health check now, for example after system resume."""
        if self._running:
            self._tick()

    def reset_after_bluez_restart(self) -> None:
        """Forget callbacks and observations owned by the previous daemon."""
        previous = dict(self._states)
        self._generation += 1
        self._connecting.clear()
        self._disconnecting.clear()
        # The old owner's live link is gone. While the profile gate is closed,
        # reject any LE link that appears under the replacement owner.
        self._le_hold_disconnect_pending = not self._le_enabled
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._le_reset_failures = 0
        self._next_le_reset_attempt = 0.0
        self._reset_classic_recovery()
        self._cancel_le_settle()
        self._states = {"bredr": None, "le": None}
        self._failures = {"bredr": 0, "le": 0}
        self._next_attempt = {"bredr": 0.0, "le": 0.0}
        self._last_errors.clear()
        if self._on_le_state is not None:
            self._on_le_state(None)
        if any(value is not None for value in previous.values()) and self._on_status:
            self._on_status()
        if self._running:
            self._tick()

    def recover_le_transport(self) -> None:
        """Request one serialized LE reset after GATT and bearer state diverge."""
        if not self._running:
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

    def recover_classic_transport(self) -> bool:
        """Cycle only BR/EDR after repeated OBEX transport failures.

        A true Bearer.BREDR1 state is not proof that its RFCOMM transport can
        still carry MAP. Serialize one targeted disconnect/reconnect cycle and
        keep healthy LE/ANCS untouched. The caller uses the return value to
        decide whether to wait for that cycle before its next profile open.
        """
        if not self._running:
            return False
        if self._classic_recovery_pending:
            return True
        now = self._clock()
        if now < self._next_classic_recovery_allowed:
            log.debug(
                "targeted BR/EDR recovery is rate-limited for another %.0fs",
                self._next_classic_recovery_allowed - now,
            )
            return False

        self._classic_recovery_cycles += 1
        cooldown = min(
            CLASSIC_RECOVERY_COOLDOWN_SECONDS
            * (2 ** (self._classic_recovery_cycles - 1)),
            BACKOFF_CAP_SECONDS,
        )
        self._next_classic_recovery_allowed = now + cooldown
        self._classic_recovery_pending = True
        self._classic_recovery_saw_disconnect = self._states["bredr"] is False
        self._classic_disconnect_attempts = 0
        self._next_classic_disconnect_attempt = 0.0
        self._failures["bredr"] = 0
        self._next_attempt["bredr"] = 0.0
        self._cancel_le_settle()
        log.warning(
            "requesting targeted iPhone BR/EDR recovery; preserving the LE bearer"
        )
        if self._classic_recovery_saw_disconnect:
            self._tick()
        else:
            self._request_classic_disconnect()
        return True

    def confirm_classic_transport(self) -> None:
        """Clear recovery penalties after MAP/PBAP prove Classic usable."""
        self._classic_recovery_cycles = 0
        self._next_classic_recovery_allowed = 0.0

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        self._connecting.clear()
        self._disconnecting.clear()
        self._le_hold_disconnect_pending = False
        self._le_dial_spent = False
        self._le_reset_pending = False
        self._reset_classic_recovery()
        self._cancel_le_settle()
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
        }

    def _tick(self) -> bool:
        if not self._running:
            return False

        log.debug("probing iPhone BR/EDR and LE bearer state")
        bredr = self._read("bredr")
        le = self._read("le")
        self._update_state("bredr", bredr)

        if not self._le_enabled and le is False:
            self._le_hold_disconnect_pending = True
        if not self._le_enabled and self._le_hold_disconnect_pending and le is True:
            # Do not publish the transient link to ANCS. It was missing when
            # the profile gate closed, so allowing it to become usable here
            # would put GATT ahead of the MAP/PBAP attempt again.
            self._request_le_disconnect(force=True)
        else:
            self._update_state("le", le)

        if self._classic_recovery_pending:
            if bredr is False:
                if not self._classic_recovery_saw_disconnect:
                    log.info("observed iPhone BR/EDR disconnect; reconnecting it")
                self._classic_recovery_saw_disconnect = True
                self._disconnecting.discard("bredr")
                self._failures["bredr"] = 0
                self._next_attempt["bredr"] = 0.0
            elif bredr is None:
                return True

            if not self._classic_recovery_saw_disconnect:
                self._request_classic_disconnect()
                return True
            if bredr is False:
                self._request_connect("bredr")
                return True
            log.info("targeted iPhone BR/EDR bearer recovery completed")
            self._classic_recovery_pending = False
            self._classic_recovery_saw_disconnect = False
            self._classic_disconnect_attempts = 0
            self._next_classic_disconnect_attempt = 0.0

        if self._le_reset_pending and self._le_enabled:
            if le is False:
                self._le_reset_pending = False
                self._le_reset_failures = 0
                self._next_le_reset_attempt = 0.0
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

    def _update_state(self, kind: str, value: bool | None) -> None:
        previous = self._states[kind]
        self._states[kind] = value
        if previous != value:
            label = "unknown" if value is None else ("connected" if value else "disconnected")
            log.debug("iPhone %s bearer state: %s", kind.upper(), label)
        if value is True:
            self._last_errors.pop(kind, None)
            self._failures[kind] = 0
            self._next_attempt[kind] = 0.0
        if kind == "le" and value is True and previous is not True:
            # An inbound LE connection is the missing presence event for a
            # Classic backoff accumulated while the phone was out of range.
            self._last_errors.pop("bredr", None)
            self._failures["bredr"] = 0
            self._next_attempt["bredr"] = 0.0
        # Repeating stale Connected=true after a failed GATT operation can
        # race BlueZ's pending CCC registration with ATT teardown. Consumers
        # therefore receive only genuine bearer lifecycle transitions.
        if previous != value and kind == "le" and self._on_le_state is not None:
            self._on_le_state(value)
        if previous != value and self._on_status is not None:
            self._on_status()

    def _request_connect(self, kind: str) -> None:
        if kind in self._connecting:
            return
        if kind == "le" and self._le_dial_exhausted():
            return
        if self._clock() < self._next_attempt[kind]:
            return
        if kind == "le":
            self._le_dial_spent = True
        self._connecting.add(kind)
        generation = self._generation
        log.info("connecting iPhone %s bearer", kind.upper())
        try:
            # Pairing pins the device to BR/EDR so iOS sees MAP/PBAP first.
            # Switch that preference explicitly for the deferred LE handoff;
            # restore it just as explicitly before a later Classic reconnect.
            self._prefer(kind)
            self._connect(
                kind,
                lambda: self._connect_succeeded(kind, generation),
                lambda error: self._connect_failed(kind, error, generation),
            )
        except Exception as error:
            self._connect_failed(kind, error, generation)

    def _connect_succeeded(self, kind: str, generation: int) -> None:
        if generation != self._generation:
            return
        self._connecting.discard(kind)
        self._last_errors.pop(kind, None)
        if kind == "le" and not self._le_enabled:
            self._le_hold_disconnect_pending = True
            self._le_dial_spent = False
            self._request_le_disconnect(force=True)
            # The request completed only after policy closed the gate. Treat
            # rejecting it as cancellation, not as a failed connection that
            # should delay the next permitted LE attempt.
            self._failures[kind] = 0
            self._next_attempt[kind] = 0.0
            return
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

    def _connect_failed(self, kind: str, error: Exception, generation: int) -> None:
        if generation != self._generation:
            return
        self._connecting.discard(kind)
        if kind == "le" and not self._le_enabled:
            # Policy closed the gate while the asynchronous request was in
            # flight.  This generation never received its permitted attempt.
            self._le_dial_spent = False
            return
        name, message = _connect_error_parts(error)
        if name in {
            "org.bluez.Error.AlreadyConnected",
            "org.bluez.Error.InProgress",
        }:
            log.debug("%s bearer connection already active", kind.upper())
            self._connect_succeeded(kind, generation)
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

    def _le_dial_exhausted(self) -> bool:
        """Yield to inbound solicitation only when it is actually on air."""
        return self._le_dial_spent and self._inbound_le_primed()

    def _backoff_cap(self, kind: str) -> int:
        if kind == "bredr" and self.le_connected:
            return LE_CONNECTED_CLASSIC_BACKOFF_CAP_SECONDS
        return BACKOFF_CAP_SECONDS

    def _request_le_disconnect(self, *, force: bool = False) -> None:
        if "le" in self._disconnecting:
            return
        if not force and self._clock() < self._next_le_reset_attempt:
            return
        self._disconnecting.add("le")
        generation = self._generation
        try:
            self._disconnect(
                "le",
                lambda: self._disconnect_succeeded(generation),
                lambda error: self._disconnect_failed(error, generation),
            )
        except Exception as error:
            self._disconnect_failed(error, generation)

    def _request_classic_disconnect(self) -> None:
        if "bredr" in self._disconnecting:
            return
        if self._clock() < self._next_classic_disconnect_attempt:
            return
        if self._classic_disconnect_attempts >= CLASSIC_RECOVERY_DISCONNECT_ATTEMPTS:
            self._abandon_classic_recovery(
                "BR/EDR stayed connected after targeted disconnect requests"
            )
            return
        self._disconnecting.add("bredr")
        generation = self._generation
        try:
            self._disconnect(
                "bredr",
                lambda: self._classic_disconnect_succeeded(generation),
                lambda error: self._classic_disconnect_failed(error, generation),
            )
        except Exception as error:
            self._classic_disconnect_failed(error, generation)

    def _classic_disconnect_succeeded(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("bredr")
        self._classic_disconnect_attempts += 1
        delay = min(
            POLL_SECONDS * (2 ** self._classic_disconnect_attempts),
            BACKOFF_CAP_SECONDS,
        )
        self._next_classic_disconnect_attempt = self._clock() + delay
        log.info(
            "iPhone BR/EDR reset requested successfully; "
            "waiting up to %ds for state change",
            delay,
        )

    def _classic_disconnect_failed(
        self,
        error: Exception,
        generation: int,
    ) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("bredr")
        name, message = _connect_error_parts(error)
        if name in {
            "org.bluez.Error.NotConnected",
            "org.bluez.Error.AlreadyDisconnected",
        } or "not connected" in message.casefold():
            self._classic_recovery_saw_disconnect = True
            self._update_state("bredr", False)
            self._failures["bredr"] = 0
            self._next_attempt["bredr"] = 0.0
            return
        if name in {
            "org.freedesktop.DBus.Error.UnknownInterface",
            "org.freedesktop.DBus.Error.UnknownMethod",
            "org.bluez.Error.NotSupported",
        }:
            self._abandon_classic_recovery(
                f"targeted BR/EDR disconnect is unavailable ({name})"
            )
            return

        self._classic_disconnect_attempts += 1
        delay = min(
            POLL_SECONDS * (2 ** self._classic_disconnect_attempts),
            BACKOFF_CAP_SECONDS,
        )
        self._next_classic_disconnect_attempt = self._clock() + delay
        detail = f"{name}: {message}" if message else name
        log.warning(
            "could not reset iPhone BR/EDR bearer: %s (next attempt in %ds)",
            detail,
            delay,
        )

    def _abandon_classic_recovery(self, reason: str) -> None:
        log.warning("abandoning targeted iPhone BR/EDR recovery: %s", reason)
        self._classic_recovery_pending = False
        self._classic_recovery_saw_disconnect = False
        self._classic_disconnect_attempts = 0
        self._next_classic_disconnect_attempt = 0.0
        self._disconnecting.discard("bredr")

    def _reset_classic_recovery(self) -> None:
        self._classic_recovery_pending = False
        self._classic_recovery_saw_disconnect = False
        self._classic_disconnect_attempts = 0
        self._next_classic_disconnect_attempt = 0.0
        self._classic_recovery_cycles = 0
        self._next_classic_recovery_allowed = 0.0
        self._disconnecting.discard("bredr")

    def _disconnect_succeeded(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("le")
        self._le_reset_failures += 1
        delay = min(
            POLL_SECONDS * (2 ** self._le_reset_failures),
            BACKOFF_CAP_SECONDS,
        )
        self._next_le_reset_attempt = self._clock() + delay
        log.info(
            "iPhone LE bearer reset requested successfully; "
            "waiting up to %ds for state change",
            delay,
        )

    def _disconnect_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation:
            return
        self._disconnecting.discard("le")
        name, message = _connect_error_parts(error)
        if name in {
            "org.bluez.Error.NotConnected",
            "org.bluez.Error.AlreadyDisconnected",
        } or "not connected" in message.casefold():
            self._le_reset_pending = False
            self._update_state("le", False)
            if self.bredr_connected and self._le_enabled:
                self._schedule_le_connect()
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
        # Device1.Connect drives the Classic bearer (Bearer.BREDR1 can be
        # marker-only); the LE bearer interface is driven directly.
        interface = "org.bluez.Device1" if kind == "bredr" else _INTERFACES[kind]
        bearer = dbus.Interface(
            get_system_bus().get_object("org.bluez", self.device_path),
            interface,
        )
        bearer.Connect(
            reply_handler=on_success,
            error_handler=on_error,
            timeout=45.0,
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
            properties.Set(
                "org.bluez.Device1",
                "PreferredBearer",
                dbus.String(kind),
                timeout=5.0,
            )
            log.debug("set iPhone PreferredBearer=%s", kind)
        except dbus.exceptions.DBusException as error:
            if preferred_bearer_unavailable(error):
                # Older or partial experimental BlueZ builds can still accept
                # inbound ANCS without exposing PreferredBearer.
                log.debug("PreferredBearer is unavailable on this BlueZ build")
                return
            raise
