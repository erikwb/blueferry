"""ANCS GATT client — subscribes to the three ANCS characteristics on a
paired iPhone, decodes Notification Source events, writes
GetNotificationAttributes commands to Control Point, parses Data Source
responses, and emits AncsEvents.

Protocol behavior established by the pairing experiments:

- We don't trust Device1.ServicesResolved as a readiness signal because
  BlueZ flips it true after BR/EDR SDP, before BLE GATT enumerates. Instead,
  we listen for ObjectManager.InterfacesAdded and wait for all three ANCS
  characteristic UUIDs to show up under the target iPhone's device path.

- StartNotify is called on Notification Source and Data Source as soon as
  they're present + we're paired. Control Point is write-only.

- For each NotificationAdded/Modified event we get on NS, we first request
  only AppIdentifier. Policy and per-app configuration are evaluated before
  requesting Title+Subtitle+Message, and the response comes back on DS.

- App display names are looked up lazily via GetAppAttributes and cached.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import dbus
import dbus.exceptions
from gi.repository import GLib

from blueferry.ancs.constants import (
    ANCS_CHAR_UUIDS,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    MESSAGES_APP_ID,
    NOTIFICATION_SOURCE_CHAR,
    CommandID,
    EventID,
)
from blueferry.ancs.events import AncsEvent
from blueferry.ancs.parsers import (
    AppAttributes,
    DataSourceAssembler,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
    build_get_app_attributes,
    build_get_notification_app_identifier,
    build_get_notification_attributes,
    parse_notification_app_identifier,
)
from blueferry.ancs.sequencer import RequestBacklog
from blueferry.bus import get_system_bus
from blueferry.limits import (
    MAX_ANCS_APP_CACHE,
    MAX_ANCS_PENDING_PER_APP,
    MAX_ANCS_REQUESTS,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15
DBUS_CALL_TIMEOUT_SECONDS = 10
SUBSCRIBE_RETRY_SECONDS = 2
AUTHORIZATION_RETRY_SECONDS = 5
MANAGER_RETRY_SECONDS = 2
BEARER_SETTLE_SECONDS = 2
TRANSPORT_RESET_SECONDS = 15

_BLUEZ_BUS_NAME = "org.bluez"
_DBUS_BUS_NAME = "org.freedesktop.DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"


@dataclass(slots=True)
class _PendingRequest:
    key: str
    packet: bytes
    assembler: DataSourceAssembler
    notification: Notification | None = None
    app_probe: bool = False
    expected_app_id: str | None = None
    authorization_probe: bool = False


_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _char_path_to_device_path(char_path: str) -> str:
    """Drop the last two segments: …/hciN/dev_XX/serviceYYYY/charZZZZ → …/dev_XX."""
    return "/".join(char_path.rsplit("/", 2)[:-2])


def _connection_was_lost(error: dbus.exceptions.DBusException) -> bool:
    name = (error.get_dbus_name() or "").casefold()
    detail = (error.get_dbus_message() or str(error)).casefold()
    return name.endswith(".notconnected") or "not connected" in detail


def _notification_is_already_stopped(
    error: dbus.exceptions.DBusException,
) -> bool:
    """Return whether BlueZ has already discarded our notify registration."""
    name = (error.get_dbus_name() or "").casefold()
    detail = (error.get_dbus_message() or str(error)).casefold()
    if name.endswith((".unknownobject", ".unknowninterface")):
        return True
    return any(
        marker in detail
        for marker in (
            "not notifying",
            "no notify session",
            "no notification session",
            "notify session not started",
        )
    )


class AncsClient:
    """Tracks ANCS characteristics under one target device and subscribes
    when all three are present.

    Idempotent: calling start() multiple times is harmless; if chars are
    already present at start time, we hook them immediately.
    """

    def __init__(
        self,
        device_path: str,
        on_event: Callable[[AncsEvent], None],
        on_status: Callable[[], None] | None = None,
        include_non_message_notifications: Callable[[], bool] | None = None,
        include_app_notification: Callable[[str], bool] | None = None,
        on_bluez_restart: Callable[[], None] | None = None,
        on_transport_failure: Callable[[], None] | None = None,
        *,
        previously_authorized: bool = False,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.device_path = device_path
        self.on_event = on_event
        self.on_status = on_status
        self._on_bluez_restart = on_bluez_restart
        self._on_transport_failure = on_transport_failure
        self._include_non_message_notifications = (
            include_non_message_notifications or (lambda: False)
        )
        self._include_app_notification = (
            include_app_notification or (lambda _app_id: True)
        )
        self._schedule = schedule
        self._cancel = cancel

        # Char path slots — set as InterfacesAdded fires
        self._ns_path: str | None = None
        self._ds_path: str | None = None
        self._cp_path: str | None = None
        self._notify_started = False
        self._authorized = False
        # Keep this across bearer/subscription resets. A silent Control Point
        # probe can mean "permission not granted yet" during onboarding, but
        # after this client has already received an authorized response it is
        # evidence that the rebuilt GATT transport is stale.
        self._was_authorized = previously_authorized
        self._bearer_connected: bool | None = None
        self._bearer_ready = False
        self._transport_blocked = False
        self._owned_notify_paths: set[str] = set()

        # In-flight per-notification attribute requests + app-name cache
        self._app_name_cache: OrderedDict[str, str] = OrderedDict()
        self._observed_app_ids: OrderedDict[str, None] = OrderedDict()
        self._pending_app_lookups: dict[str, list[NotificationAttributes]] = {}
        self._app_lookup_requested: set[str] = set()
        self._request_queue: RequestBacklog[_PendingRequest] = RequestBacklog(
            MAX_ANCS_REQUESTS
        )
        self._active_request: _PendingRequest | None = None
        self._request_timeout_id: int | None = None

        # ObjectManager watches live for the client lifetime. Characteristic
        # subscriptions are shorter-lived and are rebuilt as one unit whenever
        # BlueZ removes any part of the ANCS service.
        self._manager_signal_matches: list = []
        self._owner_signal_match = None
        self._characteristic_signal_matches: list = []
        self._bluez_owner_generation = 0
        self._bluez_owner_available = True
        self._manager_bind_in_progress = False
        self._manager_rebind_pending = False
        self._manager_retry_id: int | None = None
        self._subscribe_retry_id: int | None = None
        self._authorization_retry_id: int | None = None
        self._bearer_settle_id: int | None = None
        self._transport_reset_id: int | None = None
        self._started = False

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        log.info("ANCS client starting; watching %s", self.device_path)
        bus = get_system_bus()
        owner_match = bus.add_signal_receiver(
            self._on_bluez_owner_changed,
            dbus_interface=_DBUS_INTERFACE,
            signal_name="NameOwnerChanged",
            bus_name=_DBUS_BUS_NAME,
            arg0=_BLUEZ_BUS_NAME,
        )
        self._owner_signal_match = owner_match
        self._bluez_owner_available = True
        self._started = True
        try:
            self._bind_manager_and_rescan()
        except Exception:
            self._started = False
            self._owner_signal_match = None
            try:
                owner_match.remove()
            except Exception:
                log.debug("could not remove partial BlueZ owner watch", exc_info=True)
            raise
        if self._bearer_connected is True:
            self._schedule_bearer_settle()

    def _bind_manager_and_rescan(self) -> None:
        """Reconnect ObjectManager signals and sweep the current object tree."""
        if self._manager_bind_in_progress:
            self._manager_rebind_pending = True
            return
        self._manager_bind_in_progress = True
        try:
            while self._started and self._bluez_owner_available:
                self._manager_rebind_pending = False
                generation = self._bluez_owner_generation
                try:
                    self._bind_manager_once(generation)
                except Exception:
                    if generation == self._bluez_owner_generation:
                        raise
                if (
                    not self._manager_rebind_pending
                    and generation == self._bluez_owner_generation
                ):
                    return
        finally:
            self._manager_bind_in_progress = False

    def _bind_manager_once(self, generation: int) -> None:
        self._remove_manager_watches()
        om = dbus.Interface(
            get_system_bus().get_object(_BLUEZ_BUS_NAME, "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        matches = []
        try:
            matches.append(
                om.connect_to_signal("InterfacesAdded", self._on_iface_added)
            )
            matches.append(
                om.connect_to_signal("InterfacesRemoved", self._on_iface_removed)
            )
            # Sweep current state — ANCS chars may already exist if we're
            # restarting against a live pair.
            managed = om.GetManagedObjects(timeout=DBUS_CALL_TIMEOUT_SECONDS)
        except Exception:
            for match in matches:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not remove partial ANCS manager watch", exc_info=True)
            raise
        if (
            generation != self._bluez_owner_generation
            or not self._bluez_owner_available
        ):
            for match in matches:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not remove stale ANCS manager watch", exc_info=True)
            return
        self._manager_signal_matches = matches
        for path, ifaces in managed.items():
            self._on_iface_added(path, ifaces)
        if (
            generation != self._bluez_owner_generation
            or not self._bluez_owner_available
        ):
            self._remove_manager_watches()
            self._cancel_subscribe_retry()
            self._clear_characteristic_subscription(stop_notify=False)
            self._ns_path = self._ds_path = self._cp_path = None

    def _remove_manager_watches(self) -> None:
        for match in self._manager_signal_matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove ANCS manager watch", exc_info=True)
        self._manager_signal_matches = []

    def _on_bluez_owner_changed(self, _name, old_owner, new_owner) -> None:
        """Rebuild discovery when bluetoothd replaces its D-Bus owner.

        BlueZ can recreate cached GATT objects before dbus-python retargets an
        existing well-known-name signal match.  Connecting fresh manager
        watches and then sweeping GetManagedObjects closes both sides of that
        race.
        """
        if not self._started:
            return
        self._bluez_owner_generation += 1
        self._bluez_owner_available = bool(new_owner)
        if self._manager_bind_in_progress:
            self._manager_rebind_pending = True
        if old_owner:
            was_connected = self.connected
            log.info("BlueZ owner disappeared; resetting ANCS discovery")
            self._cancel_manager_retry()
            self._cancel_bearer_settle()
            self._cancel_transport_reset()
            self._remove_manager_watches()
            self._cancel_subscribe_retry()
            self._clear_characteristic_subscription(stop_notify=False)
            self._bearer_connected = None
            self._bearer_ready = False
            self._transport_blocked = False
            self._owned_notify_paths.clear()
            self._ns_path = self._ds_path = self._cp_path = None
            if was_connected and self.on_status is not None:
                self.on_status()
        if not new_owner:
            return
        log.info("BlueZ owner available; rebuilding ANCS discovery")
        if self._on_bluez_restart is not None:
            self._on_bluez_restart()
        if self._manager_bind_in_progress:
            log.debug("deferring ANCS discovery rebuild until the current sweep finishes")
            return
        try:
            self._bind_manager_and_rescan()
        except Exception as error:
            log.warning(
                "could not rebuild ANCS discovery after BlueZ restart: %s; "
                "retrying in %ds",
                error,
                MANAGER_RETRY_SECONDS,
            )
            self._schedule_manager_retry()

    def _schedule_manager_retry(self) -> None:
        if not self._started or self._manager_retry_id is not None:
            return
        self._manager_retry_id = self._schedule(
            MANAGER_RETRY_SECONDS,
            self._retry_manager_bind,
        )

    def _retry_manager_bind(self) -> bool:
        self._manager_retry_id = None
        if not self._started:
            return False
        try:
            self._bind_manager_and_rescan()
        except Exception as error:
            log.warning("ANCS discovery rebuild still unavailable: %s", error)
            self._schedule_manager_retry()
        return False

    def _cancel_manager_retry(self) -> None:
        if self._manager_retry_id is None:
            return
        try:
            self._cancel(self._manager_retry_id)
        except Exception:
            log.debug("could not remove ANCS manager retry", exc_info=True)
        self._manager_retry_id = None

    @property
    def subscribed(self) -> bool:
        """GATT Notification Source/Data Source subscriptions are active."""
        return self._notify_started

    @property
    def authorized(self) -> bool:
        """iOS accepted a content-free Control Point probe."""
        return self._authorized

    @property
    def connected(self) -> bool:
        # StartNotify only proves that BlueZ subscribed to the GATT
        # characteristics. iOS notification access is usable only after an
        # authorized Control Point round trip succeeds.
        return (
            self._bearer_connected is True
            and self._bearer_ready
            and not self._transport_blocked
            and self.subscribed
            and self.authorized
        )

    def observe_bearer_state(self, connected: bool | None) -> None:
        """Invalidate stale GATT state and rebuild it after an LE reconnect.

        BlueZ retains ANCS characteristic objects, and sometimes their
        ``Notifying`` property, across a physical LE disconnect. ObjectManager
        removal alone therefore cannot define the subscription lifecycle.
        """
        if connected is None:
            self._bearer_connected = None
            self._bearer_ready = False
            self._cancel_bearer_settle()
            return
        previous = self._bearer_connected
        self._bearer_connected = connected
        if not connected:
            self._bearer_ready = False
            self._cancel_bearer_settle()
            self._cancel_transport_reset()
            had_state = bool(
                self._notify_started
                or self._authorized
                or self._characteristic_signal_matches
                or self._owned_notify_paths
                or self._active_request is not None
                or self._request_queue
            )
            if not had_state:
                return
            log.info("iPhone LE bearer disconnected; resetting ANCS subscription")
            self._cancel_subscribe_retry()
            # Do not StopNotify: bluetoothd 5.87 SIGSEGVs in register_notify_cb
            # when a CCC enable completes after that registration was freed or
            # the ATT link dropped. Drop local receivers and keep ownership so
            # reconnect can rebind signals without touching the handle.
            self._clear_characteristic_subscription(stop_notify=False)
            if self.on_status is not None:
                self.on_status()
            return

        if previous is True:
            return
        if previous is False:
            log.info("iPhone LE bearer reconnected; rebuilding ANCS subscription")
            self._transport_blocked = False
            self._cancel_transport_reset()
        if self._started:
            self._schedule_bearer_settle()

    def _schedule_bearer_settle(self) -> None:
        if self._bearer_settle_id is not None or self._bearer_connected is not True:
            return
        self._bearer_settle_id = self._schedule(
            BEARER_SETTLE_SECONDS,
            self._finish_bearer_settle,
        )

    def _finish_bearer_settle(self) -> bool:
        self._bearer_settle_id = None
        if not self._started or self._bearer_connected is not True:
            return False
        self._bearer_ready = True
        self._try_subscribe()
        return False

    def _cancel_bearer_settle(self) -> None:
        if self._bearer_settle_id is None:
            return
        try:
            self._cancel(self._bearer_settle_id)
        except Exception:
            log.debug("could not remove ANCS bearer settle timer", exc_info=True)
        self._bearer_settle_id = None

    def _mark_transport_failed(self) -> None:
        """Latch a stale GATT transport until LE genuinely cycles."""
        was_connected = self.connected
        self._transport_blocked = True
        self._bearer_ready = False
        self._cancel_bearer_settle()
        self._cancel_subscribe_retry()
        self._clear_characteristic_subscription(stop_notify=False)
        if (
            self._started
            and self._bearer_connected is True
            and self._on_transport_failure is not None
            and self._transport_reset_id is None
        ):
            self._transport_reset_id = self._schedule(
                TRANSPORT_RESET_SECONDS,
                self._request_transport_reset,
            )
        if was_connected and self.on_status is not None:
            self.on_status()

    def _request_transport_reset(self) -> bool:
        self._transport_reset_id = None
        if (
            self._started
            and self._transport_blocked
            and self._bearer_connected is True
            and self._on_transport_failure is not None
        ):
            self._on_transport_failure()
        return False

    def _cancel_transport_reset(self) -> None:
        if self._transport_reset_id is None:
            return
        try:
            self._cancel(self._transport_reset_id)
        except Exception:
            log.debug("could not remove ANCS transport reset timer", exc_info=True)
        self._transport_reset_id = None

    def stop(self) -> None:
        log.info("ANCS client stopping")
        was_connected = self.connected
        self._started = False
        self._bluez_owner_available = False
        self._manager_rebind_pending = False
        self._cancel_manager_retry()
        self._cancel_subscribe_retry()
        self._cancel_authorization_retry()
        self._cancel_bearer_settle()
        self._cancel_transport_reset()
        self._clear_characteristic_subscription(
            stop_notify=self._bearer_connected is True and self._bearer_ready,
        )
        self._owned_notify_paths.clear()
        self._remove_manager_watches()
        if self._owner_signal_match is not None:
            try:
                self._owner_signal_match.remove()
            except Exception:
                log.debug("could not remove BlueZ owner watch", exc_info=True)
            self._owner_signal_match = None
        self._ns_path = self._ds_path = self._cp_path = None
        if was_connected and self.on_status is not None:
            self.on_status()

    # ---- ObjectManager event handlers -----------------------------------

    def _on_iface_added(self, path, ifaces):
        path_s = str(path)
        char = ifaces.get("org.bluez.GattCharacteristic1")
        if char is None:
            return
        uuid = str(char.get("UUID", "")).lower()
        if uuid not in ANCS_CHAR_UUIDS:
            return
        if _char_path_to_device_path(path_s) != self.device_path:
            return
        if uuid == NOTIFICATION_SOURCE_CHAR:
            self._ns_path = path_s
            log.info("ANCS Notification Source found: %s", path_s)
        elif uuid == DATA_SOURCE_CHAR:
            self._ds_path = path_s
            log.info("ANCS Data Source found:         %s", path_s)
        elif uuid == CONTROL_POINT_CHAR:
            self._cp_path = path_s
            log.info("ANCS Control Point found:       %s", path_s)
        self._try_subscribe()

    def _on_iface_removed(self, path, ifaces):
        path_s = str(path)
        for attr in ("_ns_path", "_ds_path", "_cp_path"):
            if getattr(self, attr) == path_s:
                was_connected = self.connected
                setattr(self, attr, None)
                self._cancel_subscribe_retry()
                self._cancel_authorization_retry()
                self._clear_characteristic_subscription(stop_notify=False)
                self._owned_notify_paths.discard(path_s)
                log.warning("ANCS char gone: %s", path_s)
                if was_connected and self.on_status is not None:
                    self.on_status()
                break

    def _clear_characteristic_subscription(self, *, stop_notify: bool = True) -> None:
        """Remove receivers and notification ownership before rediscovery."""
        self._cancel_authorization_retry()
        for match in self._characteristic_signal_matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove ANCS characteristic watch", exc_info=True)
        self._characteristic_signal_matches = []
        if self._notify_started and stop_notify:
            self._stop_bluez_notifications()
        self._notify_started = False
        self._authorized = False
        self._reset_requests()

    def _stop_bluez_notifications(self) -> bool:
        """Remove notification ownership created by us while ATT is up."""
        current_paths = tuple(
            path
            for path in (self._ns_path, self._ds_path)
            if path is not None and path in self._owned_notify_paths
        )
        remaining_paths = tuple(sorted(
            self._owned_notify_paths.difference(current_paths)
        ))
        stopped_all = True
        for path in current_paths + remaining_paths:
            try:
                dbus.Interface(
                    get_system_bus().get_object("org.bluez", path),
                    "org.bluez.GattCharacteristic1",
                ).StopNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
            except dbus.exceptions.DBusException as error:
                if _notification_is_already_stopped(error):
                    # BlueZ already dropped this registration; forget it so a
                    # later StartNotify is allowed.
                    self._owned_notify_paths.discard(path)
                    log.debug("ANCS notification was already stopped: %s", path)
                else:
                    stopped_all = False
                    log.debug("could not stop ANCS notification", exc_info=True)
            else:
                self._owned_notify_paths.discard(path)
        return stopped_all

    def _try_subscribe(self) -> None:
        if self._notify_started:
            return
        if self._transport_blocked:
            return
        # During first-time authorization, registering the GATT notification
        # subscriptions is part of the connection bootstrap on some iPhone
        # and controller combinations. In particular, BlueZ can expose the
        # bonded ANCS characteristics while Bearer.LE1 remains disconnected
        # and its explicit Connect method fails. Once authorization has ever
        # succeeded, retain the stricter settled-bearer requirement so stale
        # GATT objects cannot masquerade as a live subscription after a
        # reconnect.
        if self._was_authorized and (
            self._bearer_connected is not True or not self._bearer_ready
        ):
            return
        if not (self._ns_path and self._ds_path and self._cp_path):
            return
        generation = self._bluez_owner_generation
        ns_path = self._ns_path
        ds_path = self._ds_path

        def current_attempt() -> bool:
            return self._started and generation == self._bluez_owner_generation

        def remove_attempt_matches(matches) -> None:
            for match in matches:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not remove stale ANCS signal watch", exc_info=True)

        # Install receivers before StartNotify so the first value cannot arrive
        # in the gap between notification activation and signal registration.
        matches = []
        try:
            bus = get_system_bus()
            matches.append(bus.add_signal_receiver(
                self._on_ns_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                bus_name="org.bluez",
                path=ns_path,
            ))
            matches.append(bus.add_signal_receiver(
                self._on_ds_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                bus_name="org.bluez",
                path=ds_path,
            ))
            if not current_attempt():
                remove_attempt_matches(matches)
                return
            ns = dbus.Interface(
                bus.get_object("org.bluez", ns_path),
                "org.bluez.GattCharacteristic1",
            )
            ds = dbus.Interface(
                bus.get_object("org.bluez", ds_path),
                "org.bluez.GattCharacteristic1",
            )
            if self._should_start_notify(ns_path):
                ns.StartNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
                if current_attempt():
                    self._owned_notify_paths.add(ns_path)
            if not current_attempt():
                remove_attempt_matches(matches)
                return
            if self._should_start_notify(ds_path):
                ds.StartNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
                if current_attempt():
                    self._owned_notify_paths.add(ds_path)
        except dbus.exceptions.DBusException as e:
            if not current_attempt():
                remove_attempt_matches(matches)
                log.debug("discarded stale ANCS subscribe attempt after BlueZ changed owner")
                return
            log.warning("ANCS StartNotify failed: %s", e.get_dbus_name())
            for match in matches:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not roll back ANCS signal watch", exc_info=True)
            if _connection_was_lost(e) and self._was_authorized:
                self._mark_transport_failed()
            else:
                # Do not StopNotify a characteristic that succeeded before a
                # later CCC write failed. Our ownership set lets the retry
                # resume without racing ATT teardown in bluetoothd. Before
                # first authorization, a disconnected error is also retried:
                # the pending subscription is part of the LE bootstrap.
                self._schedule_subscribe_retry()
            return
        if not current_attempt():
            remove_attempt_matches(matches)
            return
        self._cancel_subscribe_retry()
        self._characteristic_signal_matches = matches
        self._notify_started = True
        log.info(
            "ANCS characteristic subscriptions active; requesting notification access"
        )
        self._queue_authorization_probe()

    def _should_start_notify(self, path: str) -> bool:
        """Skip StartNotify only when BlueZ still reports Notifying=true."""
        if path not in self._owned_notify_paths:
            return True
        return self._cached_notifying(path) is not True

    def _cached_notifying(self, path: str) -> bool | None:
        """Read BlueZ's local Notifying flag. This is not an ATT handle read."""
        try:
            props = dbus.Interface(
                get_system_bus().get_object("org.bluez", path),
                "org.freedesktop.DBus.Properties",
            )
            getter = getattr(props, "Get", None)
            if getter is None:
                return None
            return bool(
                getter(
                    "org.bluez.GattCharacteristic1",
                    "Notifying",
                    timeout=DBUS_CALL_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            log.debug("ANCS Notifying property unavailable for %s", path, exc_info=True)
            return None

    def _schedule_subscribe_retry(self) -> None:
        if (
            not self._started
            or self._subscribe_retry_id is not None
            or not (self._ns_path and self._ds_path and self._cp_path)
        ):
            return
        self._subscribe_retry_id = self._schedule(
            SUBSCRIBE_RETRY_SECONDS,
            self._retry_subscribe,
        )

    def _retry_subscribe(self) -> bool:
        self._subscribe_retry_id = None
        if self._started:
            self._try_subscribe()
        return False

    def _cancel_subscribe_retry(self) -> None:
        if self._subscribe_retry_id is None:
            return
        try:
            self._cancel(self._subscribe_retry_id)
        except Exception:
            log.debug("could not remove ANCS subscription retry", exc_info=True)
        self._subscribe_retry_id = None

    def _queue_authorization_probe(self) -> None:
        if not self._notify_started or self._authorized:
            return
        request = _PendingRequest(
            key="authorization",
            packet=build_get_app_attributes(MESSAGES_APP_ID),
            assembler=DataSourceAssembler(
                CommandID.GetAppAttributes,
                [0],
                app_id=MESSAGES_APP_ID,
            ),
            authorization_probe=True,
        )
        if self._request_queue.enqueue(request.key, request):
            self._pump_requests()

    def _schedule_authorization_retry(self) -> None:
        if (
            not self._started
            or not self._notify_started
            or self._authorized
            or self._authorization_retry_id is not None
        ):
            return
        self._authorization_retry_id = self._schedule(
            AUTHORIZATION_RETRY_SECONDS,
            self._retry_authorization,
        )
        log.info(
            "ANCS notification access is not authorized yet; retrying in %ds",
            AUTHORIZATION_RETRY_SECONDS,
        )

    def _retry_authorization(self) -> bool:
        self._authorization_retry_id = None
        self._queue_authorization_probe()
        return False

    def _cancel_authorization_retry(self) -> None:
        if self._authorization_retry_id is None:
            return
        try:
            self._cancel(self._authorization_retry_id)
        except Exception:
            log.debug("could not remove ANCS authorization retry", exc_info=True)
        self._authorization_retry_id = None

    def _mark_authorized(self) -> None:
        if self._authorized:
            return
        self._authorized = True
        self._was_authorized = True
        self._cancel_authorization_retry()
        log.info("ANCS notification access authorized for %s", self.device_path)
        if self.on_status is not None:
            self.on_status()

    # ---- Notification Source: new/modified/removed events --------------

    def _on_ns_changed(self, iface, changed, _invalidated):
        if iface != "org.bluez.GattCharacteristic1":
            return
        value = changed.get("Value")
        if value is None:
            return
        try:
            n = Notification.parse(bytes(value))
        except ValueError as e:
            log.error("NS parse failed: %s", e)
            return
        # Skip pre-existing (notifications that already existed on the
        # iPhone at our connect time — too noisy on initial subscribe).
        if n.is_preexisting:
            log.debug("ANCS preexisting event uid=%d cat=%d — skipping",
                      n.id, n.category)
            return
        if n.type == EventID.NotificationRemoved:
            log.debug("ANCS removed uid=%d", n.id)
            return
        # Added or Modified → identify the source app without content first.
        self._request_attrs(n)

    def _request_attrs(self, n: Notification) -> None:
        """Probe the source app without reading notification content."""
        pkt = build_get_notification_app_identifier(n.id)
        self._enqueue(_PendingRequest(
            key=f"notification:{n.id}",
            packet=pkt,
            assembler=DataSourceAssembler(
                CommandID.GetNotificationAttributes,
                [0],
                notification_id=n.id,
            ),
            notification=n,
            app_probe=True,
        ))

    def _request_full_attrs(self, n: Notification, app_id: str) -> None:
        # BlueFerry cannot act on ANCS action labels, so do not request them.
        pkt = build_get_notification_attributes(n.id)
        expected = [0, 1, 2, 3]
        self._enqueue(_PendingRequest(
            key=f"notification:{n.id}",
            packet=pkt,
            assembler=DataSourceAssembler(
                CommandID.GetNotificationAttributes,
                expected,
                notification_id=n.id,
            ),
            notification=n,
            expected_app_id=app_id,
        ))

    def _enqueue(self, request: _PendingRequest) -> None:
        if not self._request_queue.enqueue(request.key, request):
            log.warning("dropping duplicate/full ANCS request: %s", request.key)
            return
        self._pump_requests()

    def _pump_requests(self) -> None:
        """Write exactly one CP command; ANCS responses are strictly ordered."""
        if self._active_request is not None or not self._request_queue:
            return
        if not self._cp_path:
            return
        generation = self._bluez_owner_generation
        cp_path = self._cp_path
        request = self._request_queue.popleft()
        self._active_request = request

        def current_attempt() -> bool:
            return (
                self._started
                and generation == self._bluez_owner_generation
                and self._active_request is request
            )

        try:
            dbus.Interface(
                get_system_bus().get_object("org.bluez", cp_path),
                "org.bluez.GattCharacteristic1",
            ).WriteValue(
                [dbus.Byte(value) for value in request.packet],
                {},
                timeout=DBUS_CALL_TIMEOUT_SECONDS,
            )
        except dbus.exceptions.DBusException as error:
            if not current_attempt():
                log.debug("discarded stale ANCS write failure after BlueZ changed owner")
                return
            name = error.get_dbus_name() or type(error).__name__
            detail = error.get_dbus_message() or str(error)
            log.warning("ANCS CP WriteValue failed: %s: %s", name, detail)
            if _connection_was_lost(error) and self._was_authorized:
                self._mark_transport_failed()
                return
            self._abandon_request(request)
            self._active_request = None
            self._pump_requests()
            return
        if not current_attempt():
            log.debug("discarded stale ANCS write completion after BlueZ changed owner")
            return
        self._request_timeout_id = GLib.timeout_add_seconds(
            REQUEST_TIMEOUT_SECONDS, self._request_timed_out
        )

    def _request_timed_out(self) -> bool:
        request = self._active_request
        log.warning(
            "ANCS request timed out (command=%s)",
            request.assembler.command if request else "none",
        )
        self._request_timeout_id = None
        if request is not None and request.authorization_probe and self._was_authorized:
            log.warning(
                "previously authorized ANCS transport stopped responding; "
                "resetting the LE bearer"
            )
            self._mark_transport_failed()
            return False
        self._abandon_request(request)
        self._active_request = None
        self._pump_requests()
        return False

    def _finish_active_request(self) -> _PendingRequest | None:
        request = self._active_request
        self._active_request = None
        if request is not None:
            self._request_queue.finish(request.key)
        if self._request_timeout_id is not None:
            try:
                GLib.source_remove(self._request_timeout_id)
            except Exception:
                log.debug("could not remove ANCS request timer", exc_info=True)
            self._request_timeout_id = None
        return request

    def _abandon_request(self, request: _PendingRequest | None) -> None:
        """Release a failed request and flush app-name waiters with a fallback."""
        if request is None:
            return
        self._request_queue.finish(request.key)
        if request.authorization_probe:
            self._schedule_authorization_retry()
            return
        app_id = request.assembler.app_id
        if request.notification is not None or not app_id:
            return
        self._app_lookup_requested.discard(app_id)
        pending = self._pending_app_lookups.pop(app_id, [])
        for attrs in pending:
            self._emit(attrs, app_id)

    def _reset_requests(self) -> None:
        self._request_queue.clear()
        self._finish_active_request()
        self._pending_app_lookups.clear()
        self._app_lookup_requested.clear()

    # ---- Data Source: responses to our CP writes ------------------------

    def _on_ds_changed(self, iface, changed, _invalidated):
        if iface != "org.bluez.GattCharacteristic1":
            return
        value = changed.get("Value")
        if value is None:
            return
        request = self._active_request
        if request is None:
            log.warning("ignoring unsolicited ANCS Data Source fragment")
            return
        try:
            complete = request.assembler.feed(bytes(value))
        except ValueError as error:
            log.error("DS reassembly failed: %s", error)
            failed = self._finish_active_request()
            self._abandon_request(failed)
            self._pump_requests()
            return
        if complete is None:
            return
        self._finish_active_request()
        try:
            ev = DataSourceEvent.parse(complete)
        except ValueError as error:
            log.error("DS parse failed: %s", error)
            self._abandon_request(request)
            self._pump_requests()
            return
        if ev.type == CommandID.GetNotificationAttributes:
            if request.notification is None:
                log.error("notification response had no request metadata")
            elif request.app_probe:
                try:
                    notification_id, app_id = parse_notification_app_identifier(
                        ev.body
                    )
                except ValueError as error:
                    log.error("ANCS app identifier parse failed: %s", error)
                    self._abandon_request(request)
                    self._pump_requests()
                    return
                if notification_id != request.notification.id:
                    log.error("ANCS app identifier response id mismatch")
                elif app_id == MESSAGES_APP_ID:
                    self._request_full_attrs(request.notification, app_id)
                elif (
                    _APP_ID_RE.fullmatch(app_id)
                    and self._include_non_message_notifications()
                ):
                    included = self._include_app_notification(app_id)
                    if app_id not in self._observed_app_ids:
                        log.info(
                            "ANCS app observed: %s (%s)",
                            app_id,
                            "included" if included else "blocked",
                        )
                    self._observed_app_ids[app_id] = None
                    self._observed_app_ids.move_to_end(app_id)
                    while len(self._observed_app_ids) > MAX_ANCS_APP_CACHE:
                        self._observed_app_ids.popitem(last=False)
                    if included:
                        self._request_full_attrs(request.notification, app_id)
                    else:
                        log.debug(
                            "discarding ANCS notification blocked by app filter"
                        )
                else:
                    # The default policy never asks the iPhone for unrelated
                    # notification title, subtitle, message, or action text.
                    log.debug("discarding ANCS notification after app probe")
            else:
                try:
                    attrs = NotificationAttributes.parse(ev.body)
                except ValueError as error:
                    log.error("NotificationAttributes parse failed: %s", error)
                    self._abandon_request(request)
                    self._pump_requests()
                    return
                if attrs.app_id != request.expected_app_id:
                    log.error("ANCS notification app changed between requests")
                else:
                    self._handle_notification_attrs(attrs)
        elif ev.type == CommandID.GetAppAttributes:
            try:
                app_attrs = AppAttributes.parse(ev.body)
            except ValueError as error:
                log.error("AppAttributes parse failed: %s", error)
                self._abandon_request(request)
                self._pump_requests()
                return
            if request.authorization_probe:
                self._mark_authorized()
            self._handle_app_attrs(app_attrs)
        self._pump_requests()

    def _handle_notification_attrs(self, attrs: NotificationAttributes) -> None:
        if attrs.app_id == MESSAGES_APP_ID:
            # Messages popups are suppressed in favor of MAP, and correlation
            # does not need a human-readable app name.
            self._emit(attrs, attrs.app_id)
            return
        # If we don't have the app's display name yet, queue and ask the
        # iPhone for it. Otherwise emit immediately.
        if attrs.app_id in self._app_name_cache:
            app_name = self._app_name_cache[attrs.app_id]
            self._app_name_cache.move_to_end(attrs.app_id)
            self._emit(attrs, app_name)
        else:
            pending = self._pending_app_lookups.setdefault(attrs.app_id, [])
            if len(pending) >= MAX_ANCS_PENDING_PER_APP:
                log.warning("ANCS app lookup backlog full for %s", attrs.app_id)
                self._emit(attrs, attrs.app_id)
                return
            pending.append(attrs)
            if not self._request_app_name(attrs.app_id):
                pending.remove(attrs)
                if not pending:
                    self._pending_app_lookups.pop(attrs.app_id, None)
                self._emit(attrs, attrs.app_id)

    def _request_app_name(self, app_id: str) -> bool:
        if app_id in self._app_name_cache or app_id in self._app_lookup_requested:
            return True
        self._app_lookup_requested.add(app_id)
        request = _PendingRequest(
            key=f"app:{app_id}",
            packet=build_get_app_attributes(app_id),
            assembler=DataSourceAssembler(
                CommandID.GetAppAttributes,
                [0],
                app_id=app_id,
            ),
        )
        if not self._request_queue.enqueue(request.key, request):
            self._app_lookup_requested.discard(app_id)
            log.warning("dropping full ANCS app-name request: %s", app_id)
            return False
        self._pump_requests()
        return True

    def _handle_app_attrs(self, app_attrs: AppAttributes) -> None:
        # Display names are remote presentation data. Keep the cache bounded
        # both by entries and by each entry's size.
        app_name = app_attrs.app_name[:256]
        self._app_name_cache[app_attrs.app_id] = app_name
        self._app_name_cache.move_to_end(app_attrs.app_id)
        while len(self._app_name_cache) > MAX_ANCS_APP_CACHE:
            self._app_name_cache.popitem(last=False)
        self._app_lookup_requested.discard(app_attrs.app_id)
        pending = self._pending_app_lookups.pop(app_attrs.app_id, [])
        for attrs in pending:
            self._emit(attrs, app_name)

    def _emit(
        self,
        attrs: NotificationAttributes,
        app_name: str,
    ) -> None:
        event = AncsEvent(
            notification_id=attrs.id,
            app_id=attrs.app_id,
            app_name=app_name,
            title=attrs.title,
            subtitle=attrs.subtitle,
            body=attrs.message,
        )
        log.info("ANCS event (%d-char body)", len(event.body or ""))
        log.debug(
            "ANCS event metadata: title-chars=%d body-chars=%d",
            len(event.title or ""),
            len(event.body or ""),
        )
        try:
            self.on_event(event)
        except Exception:
            log.exception("on_event callback raised")
