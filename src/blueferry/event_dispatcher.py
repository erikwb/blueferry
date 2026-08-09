"""Message construction and fan-out to persistence, desktop, and D-Bus sinks."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from hashlib import blake2b

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.events import sms_group_sent_event, sms_sent_event
from blueferry.limits import MAX_ANCS_FINGERPRINTS
from blueferry.sinks import Sink
from blueferry.sinks.libnotify import LibnotifySink
from blueferry.sinks.sqlite import SqliteSink

log = logging.getLogger(__name__)

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
    ) -> None:
        self.contacts = contacts
        self.submit_obex = submit_obex
        self.sinks: list[Sink] = []
        self.dbus_service = None
        self.notification_policy = notification_policy
        self.storage = storage
        self.on_incoming_message = on_incoming_message
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
        if self.sinks:
            return
        self.sinks.append(SqliteSink(storage=self.storage))
        try:
            self.sinks.append(
                LibnotifySink(
                    submit_obex=self.submit_obex,
                    notification_policy=self.notification_policy,
                )
            )
        except Exception:
            log.exception("libnotify sink failed to init — continuing")
        log.info("sinks ready: %s", self.names)

    @property
    def names(self) -> list[str]:
        return [sink.name for sink in self.sinks]

    def set_dbus_service(self, service) -> None:
        self.dbus_service = service

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
