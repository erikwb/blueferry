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

- For each NotificationAdded/Modified event we get on NS, we synthesize a
  GetNotificationAttributes packet asking for AppIdentifier+Title+Subtitle+
  Message (plus positive/negative action labels if the iPhone declared
  them) and write it to CP. The response comes back on DS.

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
        *,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.device_path = device_path
        self.on_event = on_event
        self.on_status = on_status
        self._include_non_message_notifications = (
            include_non_message_notifications or (lambda: False)
        )
        self._schedule = schedule
        self._cancel = cancel

        # Char path slots — set as InterfacesAdded fires
        self._ns_path: str | None = None
        self._ds_path: str | None = None
        self._cp_path: str | None = None
        self._notify_started = False
        self._authorized = False

        # In-flight per-notification attribute requests + app-name cache
        self._app_name_cache: OrderedDict[str, str] = OrderedDict()
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
        self._characteristic_signal_matches: list = []
        self._subscribe_retry_id: int | None = None
        self._authorization_retry_id: int | None = None
        self._started = False

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        log.info("ANCS client starting; watching %s", self.device_path)
        om = dbus.Interface(
            get_system_bus().get_object("org.bluez", "/"),
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
        self._manager_signal_matches = matches
        self._started = True
        for path, ifaces in managed.items():
            self._on_iface_added(path, ifaces)

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
        return self.subscribed and self.authorized

    def stop(self) -> None:
        log.info("ANCS client stopping")
        was_connected = self.connected
        self._cancel_subscribe_retry()
        self._cancel_authorization_retry()
        self._clear_characteristic_subscription()
        for m in self._manager_signal_matches:
            try:
                m.remove()
            except Exception:
                log.debug("could not remove ANCS manager watch", exc_info=True)
        self._manager_signal_matches = []
        self._started = False
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
                self._clear_characteristic_subscription()
                log.warning("ANCS char gone: %s", path_s)
                if was_connected and self.on_status is not None:
                    self.on_status()
                break

    def _clear_characteristic_subscription(self) -> None:
        """Remove receivers and notification ownership before rediscovery."""
        self._cancel_authorization_retry()
        for match in self._characteristic_signal_matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove ANCS characteristic watch", exc_info=True)
        self._characteristic_signal_matches = []
        if self._notify_started:
            for path in (self._ns_path, self._ds_path):
                if path:
                    try:
                        dbus.Interface(
                            get_system_bus().get_object("org.bluez", path),
                            "org.bluez.GattCharacteristic1",
                        ).StopNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
                    except dbus.exceptions.DBusException:
                        pass
        self._notify_started = False
        self._authorized = False
        self._reset_requests()

    def _try_subscribe(self) -> None:
        if self._notify_started:
            return
        if not (self._ns_path and self._ds_path and self._cp_path):
            return
        # Install receivers before StartNotify so the first value cannot arrive
        # in the gap between notification activation and signal registration.
        matches = []
        started = []
        try:
            bus = get_system_bus()
            matches.append(bus.add_signal_receiver(
                self._on_ns_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                bus_name="org.bluez",
                path=self._ns_path,
            ))
            matches.append(bus.add_signal_receiver(
                self._on_ds_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                bus_name="org.bluez",
                path=self._ds_path,
            ))
            ns = dbus.Interface(
                bus.get_object("org.bluez", self._ns_path),
                "org.bluez.GattCharacteristic1",
            )
            ds = dbus.Interface(
                bus.get_object("org.bluez", self._ds_path),
                "org.bluez.GattCharacteristic1",
            )
            ns.StartNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
            started.append(ns)
            ds.StartNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
            started.append(ds)
        except dbus.exceptions.DBusException as e:
            log.warning("ANCS StartNotify failed: %s", e.get_dbus_name())
            for characteristic in started:
                try:
                    characteristic.StopNotify(timeout=DBUS_CALL_TIMEOUT_SECONDS)
                except dbus.exceptions.DBusException:
                    log.debug("could not roll back ANCS notification", exc_info=True)
            for match in matches:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not roll back ANCS signal watch", exc_info=True)
            self._schedule_subscribe_retry()
            return
        self._cancel_subscribe_retry()
        self._characteristic_signal_matches = matches
        self._notify_started = True
        log.info(
            "ANCS characteristic subscriptions active; requesting notification access"
        )
        self._queue_authorization_probe()

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
        request = self._request_queue.popleft()
        self._active_request = request
        try:
            dbus.Interface(
                get_system_bus().get_object("org.bluez", self._cp_path),
                "org.bluez.GattCharacteristic1",
            ).WriteValue(
                [dbus.Byte(value) for value in request.packet],
                {},
                timeout=DBUS_CALL_TIMEOUT_SECONDS,
            )
        except dbus.exceptions.DBusException as error:
            name = error.get_dbus_name() or type(error).__name__
            detail = error.get_dbus_message() or str(error)
            log.warning("ANCS CP WriteValue failed: %s: %s", name, detail)
            self._abandon_request(request)
            self._active_request = None
            self._pump_requests()
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
                    self._request_full_attrs(request.notification, app_id)
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
