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

Like the ANCS client, this client never calls ``StopNotify``: bluetoothd 5.87
crashes when a CCC enable completes after its registration was freed during
an LE flap (see PROTOCOL.md). Registrations are released when the daemon's
D-Bus connection closes.
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

_BLUEZ = "org.bluez"
_GATT_CHAR = "org.bluez.GattCharacteristic1"
_PROPERTIES = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"

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


class AmsClient:
    def __init__(
        self,
        device_path: str,
        *,
        on_update: Callable[[EntityUpdate], None],
        on_supported_commands: Callable[[frozenset[RemoteCommandID]], None],
        on_availability: Callable[[bool], None] | None = None,
        bus_factory: Callable[[], Any] = get_system_bus,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.device_path = device_path
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
        self._characteristic_matches: list = []
        self._operations: deque[_Operation] = deque()
        self._active: _Operation | None = None
        self._pending_reads: set[tuple[int, int]] = set()
        self._settle_id: int | None = None
        self._retry_id: int | None = None
        self._first_update_id: int | None = None
        self._retry_delay = SUBSCRIBE_RETRY_INITIAL_SECONDS

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
        self._bind_manager()
        if self._bearer_connected is True:
            self._schedule_settle()

    def stop(self) -> None:
        if not self._started:
            return
        log.info("AMS client stopping")
        self._started = False
        self._bearer_ready = False
        self._cancel_settle()
        self._reset_subscription()
        self._remove_matches(self._manager_matches)
        self._manager_generation += 1
        self._paths.clear()
        self._owned_notify_paths.clear()

    def observe_bearer_state(self, connected: bool | None) -> None:
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
        if self._started:
            self._schedule_settle()

    def observe_bluez_owner(self, old_owner, new_owner) -> None:
        if not self._started:
            return
        if old_owner:
            log.info("BlueZ owner disappeared; resetting AMS discovery")
            self._bearer_ready = False
            self._cancel_settle()
            self._reset_subscription()
            self._bearer_connected = None
            self._remove_matches(self._manager_matches)
            self._manager_generation += 1
            self._paths.clear()
            self._owned_notify_paths.clear()
        if new_owner:
            self._bind_manager()

    # ---- discovery ------------------------------------------------------

    def _bind_manager(self) -> None:
        self._remove_matches(self._manager_matches)
        bus = self._bus_factory()
        self._manager_generation += 1
        generation = self._manager_generation
        self._manager_matches = [
            bus.add_signal_receiver(
                self._on_iface_added,
                dbus_interface=_OBJECT_MANAGER,
                signal_name="InterfacesAdded",
                bus_name=_BLUEZ,
                path="/",
            ),
            bus.add_signal_receiver(
                self._on_iface_removed,
                dbus_interface=_OBJECT_MANAGER,
                signal_name="InterfacesRemoved",
                bus_name=_BLUEZ,
                path="/",
            ),
        ]

        def swept(managed) -> None:
            if not self._started or generation != self._manager_generation:
                return
            for path, interfaces in managed.items():
                self._on_iface_added(path, interfaces)

        def failed(error) -> None:
            log.warning("AMS object sweep failed: %s", _error_name(error))

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
        self._paths[uuid] = path_s
        log.info("AMS characteristic found: %s", uuid)
        self._try_subscribe()

    def _on_iface_removed(self, path, _interfaces) -> None:
        path_s = str(path)
        for uuid, known in tuple(self._paths.items()):
            if known == path_s:
                log.info("AMS characteristic removed: %s", uuid)
                del self._paths[uuid]
                self._owned_notify_paths.discard(path_s)
                self._reset_subscription()
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
        generation = self._generation
        bus = self._bus_factory()
        rc_path = self._paths[REMOTE_COMMAND_CHAR]
        eu_path = self._paths[ENTITY_UPDATE_CHAR]
        # Install receivers before StartNotify so the first value cannot be
        # lost between CCC activation and signal registration.
        self._characteristic_matches = [
            bus.add_signal_receiver(
                self._on_remote_command_changed,
                dbus_interface=_PROPERTIES,
                signal_name="PropertiesChanged",
                bus_name=_BLUEZ,
                path=rc_path,
            ),
            bus.add_signal_receiver(
                self._on_entity_update_changed,
                dbus_interface=_PROPERTIES,
                signal_name="PropertiesChanged",
                bus_name=_BLUEZ,
                path=eu_path,
            ),
        ]
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
            characteristic.StartNotify(
                dbus_interface=_GATT_CHAR,
                reply_handler=started,
                error_handler=fail,
                timeout=DBUS_CALL_TIMEOUT_SECONDS,
            )

        def started() -> None:
            self._owned_notify_paths.add(path)
            ok()

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
        self._remove_matches(self._characteristic_matches)
        failed = list(self._operations)
        if self._active is not None:
            failed.insert(0, self._active)
        self._operations.clear()
        self._active = None
        self._pending_reads.clear()
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
        self._on_supported_commands(commands)

    def _on_entity_update_changed(self, interface, changed, _invalidated) -> None:
        if interface != _GATT_CHAR or not self._started:
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            update = EntityUpdate.parse(bytes(value))
        except ValueError as error:
            log.warning("AMS entity update rejected: %s", error)
            return
        if self._first_update_id is not None:
            try:
                self._cancel(self._first_update_id)
            except Exception:
                log.debug("could not remove AMS first-update timer", exc_info=True)
            self._first_update_id = None
        # Backoff resets only once notifications are proven to flow.
        self._retry_delay = SUBSCRIBE_RETRY_INITIAL_SECONDS
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
            if generation == self._generation:
                self._deliver(EntityUpdate(entity, attribute, False, text))
            ok()

        def finished() -> None:
            self._pending_reads.discard(key)

        def failed(error: Exception) -> None:
            self._pending_reads.discard(key)
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
