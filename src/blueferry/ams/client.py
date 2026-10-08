"""AMS GATT client on the LE bond that already carries ANCS.

The iPhone publishes the Apple Media Service next to ANCS in its GATT
database. This client never opens a connection of its own: it waits for the
bearer supervisor's LE link, finds the three AMS characteristics under the
target device, subscribes to Remote Command and Entity Update, registers the
Player, Queue and Track attributes, and fetches truncated values through
Entity Attribute.

Every BlueZ call is asynchronous (``reply_handler``/``error_handler``) so the
daemon's GLib loop is never blocked, and all GATT operations are serialized
through one bounded queue. Replies that arrive after a bearer reset, a BlueZ
owner change or ``stop()`` are discarded by generation.

Like the ANCS client, this client never calls ``StopNotify`` on a dropped or
flapping link: bluetoothd 5.87 crashes when a CCC enable completes after its
registration was freed during an LE flap (see PROTOCOL.md). The one exception
is a deliberate runtime opt-out (``stop(release=True)``): iOS sends its command
list only on a CCC write, so a later opt-in must find the CCC disabled.
``AmsNotifySessions`` owns that release and defers it until the LE link is up
and settled with no ``StartNotify`` outstanding. Otherwise registrations are
released when the daemon's D-Bus connection closes.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.ams.constants import (
    AMS_CHAR_UUIDS,
    AMS_ERROR_NAMES,
    ENTITY_ATTRIBUTE_CHAR,
    ENTITY_ATTRIBUTES,
    ENTITY_UPDATE_CHAR,
    REMOTE_COMMAND_CHAR,
    EntityID,
    RemoteCommandID,
)
from blueferry.ams.parsers import (
    EntityUpdate,
    build_entity_attribute_request,
    build_entity_update_registration,
    build_remote_command,
    decode_attribute_value,
    parse_supported_commands,
)
from blueferry.bus import get_system_bus
from blueferry.limits import MAX_AMS_PENDING_OPERATIONS

log = logging.getLogger(__name__)

DBUS_CALL_TIMEOUT_SECONDS = 10
BEARER_SETTLE_SECONDS = 3
SUBSCRIBE_RETRY_INITIAL_SECONDS = 2
SUBSCRIBE_RETRY_MAX_SECONDS = 60
# The Media Source answers a registration with the current attribute values.
# Silence after a successful registration means the notifications are not
# reaching BlueFerry (for example a stale CCC registration).
FIRST_UPDATE_TIMEOUT_SECONDS = 10
# How often per LE link the silence watchdog may resubscribe. If iOS simply
# has nothing to report (no player has ever run), more attempts would only
# flap availability; a new link starts a new budget.
SILENT_RESUBSCRIBES_PER_LINK = 1
MANAGER_RETRY_INITIAL_SECONDS = 2
MANAGER_RETRY_MAX_SECONDS = 60
# A failed StopNotify is retried this often per settled LE link, with a
# doubling pause. After that the release rests until the next link-up (or a
# new opt-in) instead of looping.
RELEASE_ATTEMPTS = 3
RELEASE_RETRY_INITIAL_SECONDS = 2

_BLUEZ = "org.bluez"
_GATT_CHAR = "org.bluez.GattCharacteristic1"
_PROPERTIES = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
_NO_REPLY_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.Timeout",
    "org.freedesktop.DBus.Error.TimedOut",
})
_OBJECT_GONE_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.UnknownObject",
    "org.freedesktop.DBus.Error.UnknownMethod",
})
# BlueZ's StopNotify answer when the sender holds no session. Only the text
# tells it from a real failure; an unrecognized text is retried like one.
_NO_SESSION_MESSAGE = "no notify session started"


def _session_is_gone(error: Exception) -> bool:
    """A StopNotify error that proves there is no session left to stop."""
    if not isinstance(error, dbus.exceptions.DBusException):
        return False
    if error.get_dbus_name() in _OBJECT_GONE_ERRORS:
        return True
    return _NO_SESSION_MESSAGE in (error.get_dbus_message() or "").lower()

Success = Callable[[], None]
Failure = Callable[[Exception], None]


class AmsUnavailableError(RuntimeError):
    """AMS is not subscribed on a live LE link."""


class AmsQueueFullError(RuntimeError):
    """The serialized GATT queue is full; the phone is not keeping up."""


def _char_path_to_device_path(char_path: str) -> str:
    return "/".join(char_path.rsplit("/", 2)[:-2])


_ATT_CODE_RE = re.compile(r"0x([0-9a-f]{2})\b", re.IGNORECASE)


def _error_name(error: Exception) -> str:
    """D-Bus error name plus a named AMS ATT code when BlueZ reports one.

    Only the error name and a known AMS code are logged; the free-form
    message is never echoed.
    """
    if not isinstance(error, dbus.exceptions.DBusException):
        return type(error).__name__
    name = error.get_dbus_name() or type(error).__name__
    for match in _ATT_CODE_RE.finditer(error.get_dbus_message() or ""):
        code = int(match.group(1), 16)
        if code in AMS_ERROR_NAMES:
            return f"{name} (AMS {AMS_ERROR_NAMES[code]} 0x{code:02X})"
    return name


@dataclass(slots=True)
class _Operation:
    """One serialized GATT round trip."""

    label: str
    run: Callable[[Success, Failure], None]
    on_success: Callable[[], None]
    on_failure: Callable[[Exception], None]
    generation: int


class AmsNotifySessions:
    """BlueZ notify sessions this process may still hold on AMS characteristics.

    BlueZ keeps one notify session per D-Bus sender and characteristic. It
    outlives the ``AmsClient`` that started it, BlueZ re-enables its CCC at
    every LE link-up, and a repeated ``StartNotify`` answers without writing
    the CCC, so iOS would not send its command list again. A runtime opt-out
    therefore owes a ``StopNotify`` for every session that was started or is
    still being started. It is sent only on a settled LE link with no
    ``StartNotify`` outstanding; until then the release stays owed, and a new
    client waits for it (``when_released``). A ``StopNotify`` that fails
    leaves its session marked and is retried a bounded number of times.
    """

    def __init__(
        self,
        *,
        bus_factory: Callable[[], Any] = get_system_bus,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self._bus_factory = bus_factory
        self._schedule = schedule
        self._cancel = cancel
        # Started (or possibly started) and not confirmed stopped.
        self._alive: set[str] = set()
        # StartNotify calls without a reply yet, by token.
        self._starting: dict[int, str] = {}
        self._stopping: set[str] = set()
        self._next_token = 0
        self._owed = False
        self._waiters: list[Callable[[], None]] = []
        self._link: bool | None = None
        self._settled = False
        self._settle_id: int | None = None
        self._retry_id: int | None = None
        self._attempts_left = RELEASE_ATTEMPTS
        self._retry_delay = RELEASE_RETRY_INITIAL_SECONDS

    @property
    def release_owed(self) -> bool:
        """An opt-out's StopNotify calls are still outstanding."""
        return self._owed

    def observe_bearer_state(self, connected: bool | None) -> None:
        previous, self._link = self._link, connected
        if connected is not True:
            self._settled = False
            self._cancel_timers()
        elif previous is not True:
            self._renew_attempts()
            self._advance()

    def observe_bluez_owner(self, old_owner, _new_owner) -> None:
        if not old_owner:
            return
        # Every session belonged to the bluetoothd that just went away.
        self._alive.clear()
        self._starting.clear()
        self._stopping.clear()
        self._link = None
        self._settled = False
        self._cancel_timers()
        self._advance()

    def forget(self, path: str) -> None:
        """The characteristic object is gone, and its sessions with it."""
        self._alive.discard(path)
        self._stopping.discard(path)
        for token in [token for token, known in self._starting.items() if known == path]:
            del self._starting[token]
        self._advance()

    def starting(self, path: str) -> int:
        """A StartNotify is about to be sent; returns the token for its reply."""
        if self._owed:
            self._finish()  # a client took the sessions over again
        self._next_token += 1
        self._starting[self._next_token] = path
        return self._next_token

    def started(self, token: int) -> None:
        path = self._starting.pop(token, None)
        if path is None:
            return
        self._alive.add(path)
        self._advance()

    def start_failed(self, token: int, error: Exception) -> None:
        path = self._starting.pop(token, None)
        if path is None:
            return
        if (
            isinstance(error, dbus.exceptions.DBusException)
            and error.get_dbus_name() in _NO_REPLY_ERRORS
        ):
            # Unanswered, not refused: BlueZ may still have created it.
            self._alive.add(path)
        self._advance()

    def release(
        self,
        *,
        settled: bool = False,
        on_released: Callable[[], None] | None = None,
    ) -> None:
        """Owe a StopNotify for every session; ``settled`` vouches for the link."""
        if on_released is not None:
            self._waiters.append(on_released)
        if settled and self._link is True:
            self._settled = True
        self._owed = True
        self._renew_attempts()
        self._advance()

    def when_released(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` once no release is owed (at once if none is)."""
        if not self._owed:
            callback()
            return
        if callback not in self._waiters:
            self._waiters.append(callback)
        if self._attempts_left <= 0:
            self._renew_attempts()  # the release rested; try again for this opt-in
        self._advance()

    def close(self) -> None:
        """Daemon shutdown: the closing bus connection ends the sessions."""
        self._owed = False
        self._waiters.clear()
        self._cancel_timers()

    def _renew_attempts(self) -> None:
        self._attempts_left = RELEASE_ATTEMPTS
        self._retry_delay = RELEASE_RETRY_INITIAL_SECONDS

    def _advance(self) -> None:
        """Send the owed StopNotify calls if, and only if, it is safe now."""
        if not self._owed or self._starting or self._stopping:
            return  # the pending replies call back in here
        if not self._alive:
            self._finish()
            return
        if self._link is not True:
            return
        if not self._settled:
            # BlueZ is still re-registering notifications (see PROTOCOL.md).
            if self._settle_id is None:
                self._settle_id = self._schedule(BEARER_SETTLE_SECONDS, self._settle_elapsed)
            return
        if self._retry_id is not None:
            return
        if self._attempts_left <= 0:
            # Rest until the next link-up. An opt-in that waits goes ahead on
            # the old sessions rather than staying off; they stay marked.
            if self._waiters:
                log.warning(
                    "could not release the phone's media notifications; "
                    "its command list may be missing until the next reconnect"
                )
                self._finish()
            return
        self._attempts_left -= 1
        self._stop_notifications()

    def _finish(self) -> None:
        self._owed = False
        self._cancel_timers()
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            try:
                waiter()
            except Exception:
                log.exception("AMS release callback failed")

    def _settle_elapsed(self) -> bool:
        self._settle_id = None
        if self._link is True:
            self._settled = True
            self._advance()
        return False

    def _retry(self) -> bool:
        self._retry_id = None
        self._advance()
        return False

    def _cancel_timers(self) -> None:
        for attribute in ("_settle_id", "_retry_id"):
            source = getattr(self, attribute)
            if source is not None:
                try:
                    self._cancel(source)
                except Exception:
                    log.debug("could not remove AMS release timer", exc_info=True)
                setattr(self, attribute, None)

    def _stop_notifications(self) -> None:
        log.info("AMS opted out; releasing the phone's media notifications")
        paths = sorted(self._alive)
        self._stopping.update(paths)
        for path in paths:
            try:
                self._bus_factory().get_object(_BLUEZ, path, introspect=False).StopNotify(
                    dbus_interface=_GATT_CHAR,
                    reply_handler=lambda path=path: self._stopped(path),
                    error_handler=lambda error, path=path: self._stop_failed(path, error),
                    timeout=DBUS_CALL_TIMEOUT_SECONDS,
                )
            except Exception as error:  # a vanished object or bus
                self._stop_failed(path, error)

    def _stopped(self, path: str) -> None:
        if path not in self._stopping:
            return
        self._stopping.discard(path)
        self._alive.discard(path)
        self._stop_answered()

    def _stop_failed(self, path: str, error: Exception) -> None:
        if path not in self._stopping:
            return
        self._stopping.discard(path)
        if _session_is_gone(error):
            self._alive.discard(path)
        else:
            # Not released: the session may still exist and stays marked.
            log.warning("AMS StopNotify failed: %s", _error_name(error))
        self._stop_answered()

    def _stop_answered(self) -> None:
        if self._stopping:
            return
        if (
            self._owed and self._alive and self._link is True
            and self._attempts_left > 0 and self._retry_id is None
        ):
            delay = self._retry_delay
            self._retry_delay = delay * 2
            log.info("retrying the AMS release in %ds", delay)
            self._retry_id = self._schedule(delay, self._retry)
            return
        self._advance()


class AmsClient:
    def __init__(
        self,
        device_path: str,
        *,
        on_update: Callable[[EntityUpdate], None],
        on_supported_commands: Callable[[frozenset[RemoteCommandID]], None],
        on_availability: Callable[[bool], None] | None = None,
        sessions: AmsNotifySessions | None = None,
        bus_factory: Callable[[], Any] = get_system_bus,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.device_path = device_path
        # The daemon passes one tracker that outlives every client.
        self._sessions = sessions or AmsNotifySessions(
            bus_factory=bus_factory, schedule=schedule, cancel=cancel,
        )
        self._on_update = on_update
        self._on_supported_commands = on_supported_commands
        self._on_availability = on_availability
        self._bus_factory = bus_factory
        self._schedule = schedule
        self._cancel = cancel

        self._paths: dict[str, str] = {}
        self._started = False
        self._generation = 0
        self._manager_generation = 0
        self._bearer_connected: bool | None = None
        self._bearer_ready = False
        self._subscribing = False
        self._available = False
        self._owned_notify_paths: set[str] = set()
        self._manager_matches: list = []
        # Value receivers live as long as the characteristic object, not as
        # long as one subscription: BlueZ re-enables surviving CCCs itself at
        # LE link-up, and the phone's command list can arrive before
        # BlueFerry subscribes again.
        self._characteristic_matches: dict[str, Any] = {}
        self._operations: deque[_Operation] = deque()
        self._active: _Operation | None = None
        self._pending_reads: set[tuple[int, int]] = set()
        self._attribute_updates: dict[tuple[int, int], EntityUpdate] = {}
        self._settle_id: int | None = None
        self._retry_id: int | None = None
        self._first_update_id: int | None = None
        self._manager_retry_id: int | None = None
        self._retry_delay = SUBSCRIBE_RETRY_INITIAL_SECONDS
        self._manager_retry_delay = MANAGER_RETRY_INITIAL_SECONDS
        self._silent_resubscribes_left = SILENT_RESUBSCRIBES_PER_LINK

    # ---- public state ---------------------------------------------------

    @property
    def available(self) -> bool:
        """Subscribed and registered on the current LE link."""
        return self._available

    @property
    def characteristics_found(self) -> bool:
        return AMS_CHAR_UUIDS.issubset(self._paths)

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        log.info("AMS client starting")
        self._bind_manager_guarded()
        if self._bearer_connected is True:
            self._schedule_settle()

    def stop(
        self,
        *,
        release: bool = False,
        on_released: Callable[[], None] | None = None,
    ) -> None:
        """Stop the client; with ``release``, also disable the phone's CCCs.

        The release is owed to ``AmsNotifySessions``, which sends StopNotify
        as soon as that is safe. ``on_released`` runs once it is done (or at
        once when nothing is released), so a new client never overlaps it.
        """
        settled = self._bearer_ready
        if self._started:
            log.info("AMS client stopping")
            self._started = False
            self._bearer_ready = False
            self._cancel_settle()
            self._cancel_manager_retry()
            self._reset_subscription()
            self._forget_characteristics()
            self._remove_matches(self._manager_matches)
            self._manager_generation += 1
        if release:
            self._sessions.release(settled=settled, on_released=on_released)
        elif on_released is not None:
            on_released()

    def observe_bearer_state(self, connected: bool | None) -> None:
        self._sessions.observe_bearer_state(connected)
        previous = self._bearer_connected
        self._bearer_connected = connected
        if connected is not True:
            if previous is True or self._subscribing or self._available:
                log.info("iPhone LE bearer unavailable; resetting AMS subscription")
            self._bearer_ready = False
            self._cancel_settle()
            self._reset_subscription()
            return
        if previous is True:
            return
        self._silent_resubscribes_left = SILENT_RESUBSCRIBES_PER_LINK
        if self._started:
            self._schedule_settle()

    def observe_bluez_owner(self, old_owner, new_owner) -> None:
        self._sessions.observe_bluez_owner(old_owner, new_owner)
        if not self._started:
            return
        if old_owner:
            log.info("BlueZ owner disappeared; resetting AMS discovery")
            self._bearer_ready = False
            self._cancel_settle()
            self._cancel_manager_retry()
            self._reset_subscription()
            self._bearer_connected = None
            self._forget_characteristics()
            self._remove_matches(self._manager_matches)
            self._manager_generation += 1
        if new_owner:
            # Media control is optional: a failure here must never escape
            # into the daemon's bluetoothd-restart recovery.
            self._bind_manager_guarded()

    def _forget_characteristics(self) -> None:
        """Drop everything tied to the characteristic objects themselves."""
        had_commands = REMOTE_COMMAND_CHAR in self._paths
        for path in tuple(self._characteristic_matches):
            self._remove_characteristic_match(path)
        self._paths.clear()
        self._owned_notify_paths.clear()
        if had_commands:
            self._report_supported_commands(frozenset())

    # ---- discovery ------------------------------------------------------

    def _bind_manager_guarded(self) -> None:
        try:
            self._bind_manager()
        except Exception as error:
            log.warning("AMS discovery could not start: %s", _error_name(error))
            self._schedule_manager_retry()

    def _schedule_manager_retry(self) -> None:
        if not self._started or self._manager_retry_id is not None:
            return
        delay = self._manager_retry_delay
        self._manager_retry_delay = min(delay * 2, MANAGER_RETRY_MAX_SECONDS)
        log.info("retrying AMS discovery in %ds", delay)
        self._manager_retry_id = self._schedule(delay, self._retry_manager)

    def _retry_manager(self) -> bool:
        self._manager_retry_id = None
        if self._started:
            self._bind_manager_guarded()
        return False

    def _cancel_manager_retry(self) -> None:
        if self._manager_retry_id is None:
            return
        try:
            self._cancel(self._manager_retry_id)
        except Exception:
            log.debug("could not remove AMS discovery retry", exc_info=True)
        self._manager_retry_id = None

    def _bind_manager(self) -> None:
        self._remove_matches(self._manager_matches)
        bus = self._bus_factory()
        self._manager_generation += 1
        generation = self._manager_generation
        # Appended one by one so a failure halfway leaves nothing untracked.
        for handler, signal_name in (
            (self._on_iface_added, "InterfacesAdded"),
            (self._on_iface_removed, "InterfacesRemoved"),
        ):
            self._manager_matches.append(bus.add_signal_receiver(
                handler,
                dbus_interface=_OBJECT_MANAGER,
                signal_name=signal_name,
                bus_name=_BLUEZ,
                path="/",
            ))

        def swept(managed) -> None:
            if not self._started or generation != self._manager_generation:
                return
            self._manager_retry_delay = MANAGER_RETRY_INITIAL_SECONDS
            for path, interfaces in managed.items():
                self._on_iface_added(path, interfaces)

        def failed(error) -> None:
            if not self._started or generation != self._manager_generation:
                return
            log.warning("AMS object sweep failed: %s", _error_name(error))
            # The signal watches stay; only the initial sweep is repeated.
            self._schedule_manager_retry()

        bus.get_object(_BLUEZ, "/", introspect=False).GetManagedObjects(
            dbus_interface=_OBJECT_MANAGER,
            reply_handler=swept,
            error_handler=failed,
            timeout=DBUS_CALL_TIMEOUT_SECONDS,
        )

    def _on_iface_added(self, path, interfaces) -> None:
        if not self._started:
            return
        characteristic = interfaces.get(_GATT_CHAR)
        if characteristic is None:
            return
        uuid = str(characteristic.get("UUID", "")).lower()
        path_s = str(path)
        if uuid not in AMS_CHAR_UUIDS or _char_path_to_device_path(path_s) != self.device_path:
            return
        if self._paths.get(uuid) == path_s:
            return
        previous = self._paths.get(uuid)
        if previous is not None:
            self._remove_characteristic_match(previous)
            self._owned_notify_paths.discard(previous)
            self._sessions.forget(previous)
        self._paths[uuid] = path_s
        log.info("AMS characteristic found: %s", uuid)
        self._watch_characteristic(uuid, path_s)
        self._try_subscribe()

    def _watch_characteristic(self, uuid: str, path: str) -> None:
        """Install the value receiver before any StartNotify can complete."""
        handler = {
            REMOTE_COMMAND_CHAR: self._on_remote_command_changed,
            ENTITY_UPDATE_CHAR: self._on_entity_update_changed,
        }.get(uuid)
        if handler is None or path in self._characteristic_matches:
            return
        try:
            self._characteristic_matches[path] = self._bus_factory().add_signal_receiver(
                handler,
                dbus_interface=_PROPERTIES,
                signal_name="PropertiesChanged",
                bus_name=_BLUEZ,
                path=path,
            )
        except Exception as error:
            # Without the receiver, subscribing would only lose values.
            log.warning("could not watch AMS characteristic: %s", _error_name(error))
            del self._paths[uuid]
            self._schedule_manager_retry()

    def _remove_characteristic_match(self, path: str) -> None:
        match = self._characteristic_matches.pop(path, None)
        if match is None:
            return
        try:
            match.remove()
        except Exception:
            log.debug("could not remove AMS signal watch", exc_info=True)

    def _on_iface_removed(self, path, _interfaces) -> None:
        path_s = str(path)
        for uuid, known in tuple(self._paths.items()):
            if known == path_s:
                log.info("AMS characteristic removed: %s", uuid)
                del self._paths[uuid]
                self._owned_notify_paths.discard(path_s)
                self._sessions.forget(path_s)
                self._remove_characteristic_match(path_s)
                self._reset_subscription()
                if uuid == REMOTE_COMMAND_CHAR:
                    # A new characteristic object starts a new command list.
                    self._report_supported_commands(frozenset())
                return

    # ---- subscription ---------------------------------------------------

    def _schedule_settle(self) -> None:
        if self._settle_id is not None:
            return
        self._settle_id = self._schedule(BEARER_SETTLE_SECONDS, self._settled)

    def _cancel_settle(self) -> None:
        """Only a bearer or BlueZ change invalidates the settle window."""
        if self._settle_id is None:
            return
        try:
            self._cancel(self._settle_id)
        except Exception:
            log.debug("could not remove AMS settle timer", exc_info=True)
        self._settle_id = None

    def _settled(self) -> bool:
        self._settle_id = None
        if self._started and self._bearer_connected is True:
            self._bearer_ready = True
            self._try_subscribe()
        return False

    def _try_subscribe(self) -> None:
        if (
            not self._started
            or self._subscribing
            or self._available
            or self._retry_id is not None
            or self._bearer_connected is not True
            or not self._bearer_ready
            or not self.characteristics_found
        ):
            return
        self._subscribing = True
        try:
            self._run_subscription()
        except Exception as error:
            # Never leave _subscribing stuck: that would block every later
            # attempt until the next bearer transition.
            log.warning("AMS subscription could not start: %s", _error_name(error))
            self._reset_subscription()
            self._schedule_retry()

    def _run_subscription(self) -> None:
        generation = self._generation
        rc_path = self._paths[REMOTE_COMMAND_CHAR]
        eu_path = self._paths[ENTITY_UPDATE_CHAR]
        # The value receivers were installed when the characteristics were
        # found (see _watch_characteristic), before any StartNotify.
        steps: list[tuple[str, Callable[[Success, Failure], None]]] = [
            ("start-notify remote-command", lambda ok, fail: self._start_notify(rc_path, ok, fail)),
            ("start-notify entity-update", lambda ok, fail: self._start_notify(eu_path, ok, fail)),
        ]
        def register(packet: bytes) -> Callable[[Success, Failure], None]:
            return lambda ok, fail: self._write(eu_path, packet, ok, fail)

        for entity in EntityID:
            steps.append((
                f"register {entity.name}",
                register(build_entity_update_registration(entity, ENTITY_ATTRIBUTES[entity])),
            ))
        remaining = deque(steps)

        def next_step() -> None:
            if generation != self._generation:
                return
            if not remaining:
                self._subscribing = False
                self._set_available(True)
                log.info("AMS media updates registered")
                self._first_update_id = self._schedule(
                    FIRST_UPDATE_TIMEOUT_SECONDS, self._first_update_missing,
                )
                return
            label, run = remaining.popleft()
            if not self._enqueue(_Operation(label, run, next_step, failed, generation)):
                failed(AmsQueueFullError("AMS operation queue is full"))

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            log.warning("AMS subscription step failed: %s", _error_name(error))
            self._reset_subscription()
            # A lost ATT transport normally comes with a bearer transition
            # that restarts subscription; the bounded retry also covers a
            # bearer observation that has not caught up yet.
            self._schedule_retry()

        next_step()

    def _start_notify(self, path: str, ok: Success, fail: Failure) -> None:
        characteristic = self._characteristic(path)
        generation = self._generation

        def start() -> None:
            if generation != self._generation:
                return  # reset while BlueZ answered the Notifying query
            # The session outlives this client: the tracker hears every
            # reply, also one that arrives after stop().
            token = self._sessions.starting(path)

            def started() -> None:
                self._sessions.started(token)
                self._owned_notify_paths.add(path)
                ok()

            def failed(error: Exception) -> None:
                self._sessions.start_failed(token, error)
                fail(error)

            try:
                characteristic.StartNotify(
                    dbus_interface=_GATT_CHAR,
                    reply_handler=started,
                    error_handler=failed,
                    timeout=DBUS_CALL_TIMEOUT_SECONDS,
                )
            except Exception as error:
                self._sessions.start_failed(token, error)
                raise

        if path not in self._owned_notify_paths:
            start()
            return

        # Leave a surviving CCC registration alone (see module docstring).
        def notifying(value) -> None:
            if bool(value):
                ok()
            else:
                start()

        characteristic.Get(
            _GATT_CHAR, "Notifying",
            dbus_interface=_PROPERTIES,
            reply_handler=notifying,
            error_handler=lambda _error: start(),
            timeout=DBUS_CALL_TIMEOUT_SECONDS,
        )

    def _schedule_retry(self) -> None:
        if not self._started or self._retry_id is not None:
            return
        delay = self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, SUBSCRIBE_RETRY_MAX_SECONDS)
        log.info("retrying AMS subscription in %ds", delay)
        self._retry_id = self._schedule(delay, self._retry)

    def _first_update_missing(self) -> bool:
        self._first_update_id = None
        if not self._available:
            return False
        if self._silent_resubscribes_left <= 0:
            # A fresh subscription stayed silent too. Treat it as an idle
            # Media Source instead of flapping availability.
            log.info("no AMS entity update yet; assuming no active player")
            return False
        self._silent_resubscribes_left -= 1
        log.warning(
            "no AMS entity update within %ds of registering; resubscribing",
            FIRST_UPDATE_TIMEOUT_SECONDS,
        )
        # Ask BlueZ for the notify session again instead of trusting the
        # cached Notifying flag that may describe a dead registration.
        self._owned_notify_paths.clear()
        self._reset_subscription()
        self._schedule_retry()
        return False

    def _retry(self) -> bool:
        self._retry_id = None
        self._try_subscribe()
        return False

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        if self._on_availability is not None:
            try:
                self._on_availability(available)
            except Exception:
                log.exception("AMS availability callback failed")

    def _reset_subscription(self) -> None:
        """Forget every in-flight operation; late replies are discarded."""
        self._generation += 1
        self._subscribing = False
        # The settle timer belongs to the bearer, not to this subscription:
        # a characteristic can vanish and return while the link settles.
        for attribute in ("_retry_id", "_first_update_id"):
            source = getattr(self, attribute)
            if source is not None:
                try:
                    self._cancel(source)
                except Exception:
                    log.debug("could not remove AMS timer", exc_info=True)
                setattr(self, attribute, None)
        failed = list(self._operations)
        if self._active is not None:
            failed.insert(0, self._active)
        self._operations.clear()
        self._active = None
        self._pending_reads.clear()
        self._attribute_updates.clear()
        for operation in failed:
            if operation.label.startswith("command"):
                try:
                    operation.on_failure(AmsUnavailableError("the iPhone media link was reset"))
                except Exception:
                    log.exception("AMS command failure callback raised")
        self._set_available(False)

    def _remove_matches(self, matches: list) -> None:
        for match in matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove AMS signal watch", exc_info=True)
        matches.clear()

    # ---- serialized GATT operations -------------------------------------

    def _characteristic(self, path: str):
        return self._bus_factory().get_object(_BLUEZ, path, introspect=False)

    def _write(self, path: str, packet: bytes, ok: Success, fail: Failure) -> None:
        self._characteristic(path).WriteValue(
            dbus.Array([dbus.Byte(value) for value in packet], signature="y"),
            dbus.Dictionary({}, signature="sv"),
            dbus_interface=_GATT_CHAR,
            reply_handler=ok,
            error_handler=fail,
            timeout=DBUS_CALL_TIMEOUT_SECONDS,
        )

    def _enqueue(self, operation: _Operation) -> bool:
        if len(self._operations) >= MAX_AMS_PENDING_OPERATIONS:
            return False
        self._operations.append(operation)
        self._pump()
        return True

    def _pump(self) -> None:
        if self._active is not None or not self._operations:
            return
        operation = self._operations.popleft()
        self._active = operation

        def current() -> bool:
            return self._active is operation and operation.generation == self._generation

        def succeeded(*_args) -> None:
            if not current():
                return
            self._active = None
            try:
                operation.on_success()
            finally:
                self._pump()

        def failed(error: Exception) -> None:
            if not current():
                return
            self._active = None
            try:
                operation.on_failure(error)
            finally:
                self._pump()

        try:
            operation.run(succeeded, failed)
        except Exception as error:
            failed(error)

    # ---- notifications --------------------------------------------------

    def _on_remote_command_changed(self, interface, changed, _invalidated) -> None:
        if interface != _GATT_CHAR or not self._started:
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            commands = parse_supported_commands(bytes(value))
        except ValueError as error:
            log.warning("AMS supported-command list rejected: %s", error)
            return
        log.debug("AMS supported commands: %d", len(commands))
        # Accepted even during the bearer settle window: BlueZ re-enables the
        # CCC at link-up and iOS answers with its list before BlueFerry
        # subscribes again. It is metadata, not track content.
        self._notifications_flow()
        self._report_supported_commands(commands)

    def _report_supported_commands(self, commands: frozenset[RemoteCommandID]) -> None:
        try:
            self._on_supported_commands(commands)
        except Exception:
            log.exception("AMS supported-command callback failed")

    def _notifications_flow(self) -> None:
        """Any notification proves the CCC path; disarm the silence watchdog."""
        if self._first_update_id is not None:
            try:
                self._cancel(self._first_update_id)
            except Exception:
                log.debug("could not remove AMS first-update timer", exc_info=True)
            self._first_update_id = None
        # Backoff resets only once notifications are proven to flow.
        self._retry_delay = SUBSCRIBE_RETRY_INITIAL_SECONDS

    def _on_entity_update_changed(self, interface, changed, _invalidated) -> None:
        # Track values belong to a live link; the projection was cleared when
        # the bearer dropped, so a stray value must not repopulate it.
        if interface != _GATT_CHAR or not self._started or self._bearer_connected is not True:
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            update = EntityUpdate.parse(bytes(value))
        except ValueError as error:
            log.warning("AMS entity update rejected: %s", error)
            return
        self._notifications_flow()
        key = (update.entity, update.attribute)
        if self._attribute_updates.get(key) != update:
            self._attribute_updates[key] = update
        self._deliver(update)
        if update.truncated:
            self._fetch_full_value(update.entity, update.attribute)

    def _deliver(self, update: EntityUpdate) -> None:
        try:
            self._on_update(update)
        except Exception:
            log.exception("AMS update callback failed")

    def _fetch_full_value(self, entity: int, attribute: int) -> None:
        """Read the complete value of a truncated attribute exactly once."""
        key = (entity, attribute)
        path = self._paths.get(ENTITY_ATTRIBUTE_CHAR)
        if path is None or key in self._pending_reads:
            return
        try:
            selector = build_entity_attribute_request(entity, attribute)
        except ValueError:
            return
        generation = self._generation

        # A full read belongs to the notification that requested it. Other
        # attributes may change independently while the read is in flight.
        requested = self._attribute_updates.get(key)

        def run(ok: Success, fail: Failure) -> None:
            def read_back() -> None:
                if generation != self._generation:
                    return  # the link was reset between write and read
                self._characteristic(path).ReadValue(
                    dbus.Dictionary({}, signature="sv"),
                    dbus_interface=_GATT_CHAR,
                    reply_handler=lambda value: received(value, ok, fail),
                    error_handler=fail,
                    timeout=DBUS_CALL_TIMEOUT_SECONDS,
                )

            # The selector write and the read must be adjacent; running both
            # inside one queued operation keeps any other write out between.
            self._write(path, selector, read_back, fail)

        def received(value, ok: Success, fail: Failure) -> None:
            try:
                text = decode_attribute_value(bytes(value))
            except ValueError as error:
                fail(error)
                return
            if generation == self._generation and self._attribute_updates.get(key) is requested:
                self._deliver(EntityUpdate(entity, attribute, False, text))
            ok()

        def finished() -> None:
            if generation != self._generation:
                return
            self._pending_reads.discard(key)
            latest = self._attribute_updates.get(key)
            if latest is not requested and latest is not None and latest.truncated:
                self._fetch_full_value(entity, attribute)

        def failed(error: Exception) -> None:
            finished()
            log.info(
                "AMS full attribute read failed (entity=%d attribute=%d): %s",
                entity, attribute, _error_name(error),
            )

        self._pending_reads.add(key)
        if not self._enqueue(_Operation(
            "attribute", run, finished, failed, generation,
        )):
            self._pending_reads.discard(key)
            log.warning("AMS operation queue full; keeping truncated value")

    # ---- commands -------------------------------------------------------

    def send_command(
        self,
        command: RemoteCommandID,
        on_success: Success,
        on_failure: Failure,
    ) -> None:
        """Write one Remote Command; the caller validates availability first."""
        if not self._available:
            on_failure(AmsUnavailableError("iPhone media control is not connected"))
            return
        path = self._paths.get(REMOTE_COMMAND_CHAR)
        if path is None:
            on_failure(AmsUnavailableError("iPhone media control is not connected"))
            return
        packet = build_remote_command(command)

        def failed(error: Exception) -> None:
            log.info("AMS command %s failed: %s", command.name, _error_name(error))
            on_failure(error)

        operation = _Operation(
            f"command {command.name}",
            lambda ok, fail: self._write(path, packet, ok, fail),
            on_success,
            failed,
            self._generation,
        )
        if not self._enqueue(operation):
            on_failure(AmsQueueFullError("too many pending iPhone media commands"))
