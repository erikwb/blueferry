"""Message construction and fan-out to persistence, desktop, and D-Bus sinks."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from hashlib import blake2b

from gi.repository import GLib

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.bus import get_session_bus
from blueferry.events import sms_group_sent_event, sms_sent_event
from blueferry.limits import MAX_ANCS_FINGERPRINTS
from blueferry.sinks import Sink
from blueferry.sinks.libnotify import LibnotifySink
from blueferry.sinks.sqlite import SqliteSink

log = logging.getLogger(__name__)

_NOTIFICATION_BUS_NAME = "org.freedesktop.Notifications"
_DBUS_BUS_NAME = "org.freedesktop.DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"
_LIBNOTIFY_RETRY_SECONDS = 5

_ANCS_FINGERPRINT_FIELDS = (
    "notification_id",
    "app_id",
    "title",
    "subtitle",
    "body",
)


def _ancs_fingerprint(event) -> bytes:
    if isinstance(event, dict):
        values = [event.get(field) for field in _ANCS_FINGERPRINT_FIELDS]
    else:
        values = [getattr(event, field, None) for field in _ANCS_FINGERPRINT_FIELDS]
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return blake2b(encoded, digest_size=16).digest()


class EventDispatcher:
    def __init__(
        self,
        contacts,
        *,
        submit_obex,
        historical_ancs=(),
        notification_policy=None,
        storage=None,
        on_incoming_message=None,
        notification_sink_factory: Callable[..., Sink] = LibnotifySink,
        session_bus=None,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.contacts = contacts
        self.submit_obex = submit_obex
        self.sinks: list[Sink] = []
        self.dbus_service = None
        self.notification_policy = notification_policy
        self.storage = storage
        self.on_incoming_message = on_incoming_message
        self._notification_sink_factory = notification_sink_factory
        self._session_bus = session_bus
        self._schedule = schedule
        self._cancel = cancel
        self._notification_owner_match = None
        self._notification_retry_id: int | None = None
        self._setup_complete = False
        self._setup_logged = False
        self._seen_ancs: OrderedDict[bytes, None] = OrderedDict()
        self.seed_historical_ancs(historical_ancs)

    def seed_historical_ancs(self, events) -> None:
        """Add retained correlation fingerprints without emitting events."""
        for event in events:
            if isinstance(event, dict) and event.get("kind") == "ancs_notification":
                self._seen_ancs[_ancs_fingerprint(event)] = None
        while len(self._seen_ancs) > MAX_ANCS_FINGERPRINTS:
            self._seen_ancs.popitem(last=False)

    def setup(self) -> None:
        if self._setup_complete:
            return
        self.sinks.append(SqliteSink(storage=self.storage))
        self._setup_complete = True
        self._watch_notification_owner()
        self._ensure_libnotify_sink()
        log.info("sinks ready: %s", self.names)
        self._setup_logged = True

    def stop(self) -> None:
        """Release notification-service watches and transient sink state."""
        self._setup_complete = False
        self._cancel_notification_retry()
        if self._notification_owner_match is not None:
            try:
                self._notification_owner_match.remove()
            except Exception:
                log.debug("could not remove notification owner watch", exc_info=True)
            self._notification_owner_match = None
        self._remove_libnotify_sink(log_change=False)

    def _watch_notification_owner(self) -> None:
        if self._notification_owner_match is not None:
            return
        try:
            bus = self._session_bus or get_session_bus()
            self._session_bus = bus
            self._notification_owner_match = bus.add_signal_receiver(
                self._on_notification_owner_changed,
                dbus_interface=_DBUS_INTERFACE,
                signal_name="NameOwnerChanged",
                bus_name=_DBUS_BUS_NAME,
                arg0=_NOTIFICATION_BUS_NAME,
            )
        except Exception:
            log.exception(
                "could not watch the desktop notification service; "
                "late libnotify activation is unavailable"
            )

    def _on_notification_owner_changed(self, _name, old_owner, new_owner) -> None:
        if not self._setup_complete:
            return
        if old_owner:
            self._cancel_notification_retry()
            self._remove_libnotify_sink(log_change=True)
        if new_owner:
            self._ensure_libnotify_sink()

    def _ensure_libnotify_sink(self) -> bool:
        if not self._setup_complete:
            return False
        if any(sink.name == "libnotify" for sink in self.sinks):
            return True
        try:
            sink = self._notification_sink_factory(
                submit_obex=self.submit_obex,
                notification_policy=self.notification_policy,
                on_open_message=self._open_message,
            )
        except Exception:
            log.exception("libnotify sink failed to init — continuing")
            if self._notification_server_owned():
                self._schedule_notification_retry()
            return False
        self.sinks.append(sink)
        self._cancel_notification_retry()
        if self._setup_logged:
            log.info("desktop notification service available; sinks ready: %s", self.names)
        return True

    def _remove_libnotify_sink(self, *, log_change: bool) -> None:
        removed = [sink for sink in self.sinks if sink.name == "libnotify"]
        if not removed:
            return
        self.sinks = [sink for sink in self.sinks if sink.name != "libnotify"]
        for sink in removed:
            close = getattr(sink, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                log.debug("could not close libnotify sink", exc_info=True)
        if log_change:
            log.info("desktop notification service unavailable; sinks ready: %s", self.names)

    def _notification_server_owned(self) -> bool:
        try:
            bus = self._session_bus or get_session_bus()
            self._session_bus = bus
            return bool(bus.name_has_owner(_NOTIFICATION_BUS_NAME))
        except Exception:
            log.debug("could not query desktop notification owner", exc_info=True)
            return False

    def _schedule_notification_retry(self) -> None:
        if not self._setup_complete or self._notification_retry_id is not None:
            return
        self._notification_retry_id = self._schedule(
            _LIBNOTIFY_RETRY_SECONDS,
            self._retry_libnotify_sink,
        )

    def _retry_libnotify_sink(self) -> bool:
        self._notification_retry_id = None
        if self._setup_complete and self._notification_server_owned():
            self._ensure_libnotify_sink()
        return False

    def _cancel_notification_retry(self) -> None:
        if self._notification_retry_id is None:
            return
        try:
            self._cancel(self._notification_retry_id)
        except Exception:
            log.debug("could not remove libnotify retry timer", exc_info=True)
        self._notification_retry_id = None

    @property
    def names(self) -> list[str]:
        return [sink.name for sink in self.sinks]

    def set_dbus_service(self, service) -> None:
        self.dbus_service = service

    def _open_message(self, handle: str) -> None:
        if self.dbus_service is not None:
            self.dbus_service.emit_open_message(handle)

    def message(self, event) -> None:
        if getattr(event, "kind", "") == "sms_received" and self.on_incoming_message is not None:
            self.on_incoming_message()
        for sink in self.sinks:
            try:
                sink.handle(event)
            except Exception:
                log.exception("sink %s failed on event %s", sink.name, event.handle)
        if self.dbus_service is not None:
            self.dbus_service.emit_history_changed()

    def sent(self, recipient: str, body: str, transfer_path: str) -> None:
        event = sms_sent_event(
            recipient,
            body,
            contact_name=self.contacts.resolve(recipient),
            transfer_path=transfer_path,
        )
        log.info("sms_sent (%d-char body)", len(body or ""))
        self.message(event)

    def group_sent(
        self,
        recipients: list[str],
        group_key: str,
        group_name: str,
        body: str,
        transfer_path: str,
        group_members: list[str],
    ) -> None:
        event = sms_group_sent_event(
            recipients,
            body,
            group_key=group_key,
            group_name=group_name,
            group_members=group_members,
            transfer_path=transfer_path,
        )
        log.info(
            "group sms_sent (%d recipients, %d-char body)",
            len(recipients),
            len(body or ""),
        )
        self.message(event)

    def ancs(self, event) -> None:
        fingerprint = _ancs_fingerprint(event)
        if fingerprint in self._seen_ancs:
            log.info(
                "suppressing replayed ANCS event uid=%d",
                event.notification_id,
            )
            return
        self._seen_ancs[fingerprint] = None
        self._seen_ancs.move_to_end(fingerprint)
        while len(self._seen_ancs) > MAX_ANCS_FINGERPRINTS:
            self._seen_ancs.popitem(last=False)
        for sink in self.sinks:
            if event.app_id != MESSAGES_APP_ID and not bool(
                getattr(sink, "accepts_system_ancs", False)
            ):
                continue
            try:
                handler = getattr(sink, "handle_ancs", None)
                if handler is not None:
                    handler(event)
            except Exception:
                log.exception(
                    "sink %s failed on ANCS event %d",
                    sink.name,
                    event.notification_id,
                )
        # Non-Messages ANCS events are an optional live desktop-popup stream.
        # No ANCS content is published on D-Bus. Messages correlation records
        # only trigger a content-free history invalidation after persistence.
        if self.dbus_service is not None and event.app_id == MESSAGES_APP_ID:
            self.dbus_service.emit_history_changed()
