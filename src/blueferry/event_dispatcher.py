"""Message construction and fan-out to persistence, desktop, and D-Bus sinks."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from hashlib import blake2b

from gi.repository import GLib

from blueferry import config
from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.bus import get_session_bus
from blueferry.client_activation import request_message_activation
from blueferry.events import sms_group_sent_event, sms_sent_event
from blueferry.limits import MAX_ANCS_FINGERPRINTS
from blueferry.notification_open import request_open_target
from blueferry.sinks import Sink
from blueferry.sinks.libnotify import LibnotifySink
from blueferry.sinks.sqlite import SqliteSink

_OTP_SINK_NAME = "otp-clipboard"


def _default_otp_sink(
    *, notification_policy, session_bus=None, amend_message_popup=None, resolve_metadata=None
) -> Sink:
    from blueferry.otp_clipboard import ClipboardWriter
    from blueferry.sinks.otp_clipboard import DesktopNotifier, OtpClipboardSink

    return OtpClipboardSink(
        writer=ClipboardWriter(clear_after_s=config.OTP_CLEAR_SECONDS),
        notification_policy=notification_policy,
        notifier=DesktopNotifier(session_bus),
        amend_message_popup=amend_message_popup,
        resolve_metadata=resolve_metadata,
    )


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
        defer_mark_read: Callable[[str], None],
        historical_ancs=(),
        notification_policy=None,
        contacts_only_notifications=None,
        notification_open_target=None,
        storage=None,
        on_incoming_message=None,
        contact_photo: Callable[[str | None], str | None] | None = None,
        perform_ancs_action=None,
        ancs_actions_enabled: Callable[[], bool] | None = None,
        on_call_action: Callable[[str, str], None] | None = None,
        notification_sink_factory: Callable[..., Sink] = LibnotifySink,
        otp_autocopy: Callable[[], bool] | None = None,
        otp_sink_factory: Callable[..., Sink] = _default_otp_sink,
        resolve_otp_metadata=None,
        session_bus=None,
        schedule: Callable[[int, Callable[[], bool]], int] = GLib.timeout_add_seconds,
        cancel: Callable[[int], object] = GLib.source_remove,
    ) -> None:
        self.contacts = contacts
        self.defer_mark_read = defer_mark_read
        self.sinks: list[Sink] = []
        self.dbus_service = None
        self.notification_policy = notification_policy
        self.contacts_only_notifications = contacts_only_notifications
        self.notification_open_target = notification_open_target
        self.storage = storage
        self.on_incoming_message = on_incoming_message
        self.contact_photo = contact_photo
        self.perform_ancs_action = perform_ancs_action
        self.ancs_actions_enabled = ancs_actions_enabled
        self.on_call_action = on_call_action
        self._notification_sink_factory = notification_sink_factory
        self._otp_autocopy = otp_autocopy
        self._otp_sink_factory = otp_sink_factory
        self._resolve_otp_metadata = resolve_otp_metadata
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
        self._ensure_otp_sink()
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
        # Shutdown: the server is still ours, so retire live iPhone action
        # buttons instead of leaving them wired to a daemon that is gone.
        self.ancs_actions_reset()
        self._remove_libnotify_sink(log_change=False)
        self._remove_sinks(_OTP_SINK_NAME)

    def _ensure_otp_sink(self) -> None:
        """Add the opt-in one-time code clipboard sink."""
        enabled = (
            self._otp_autocopy() if self._otp_autocopy is not None else config.OTP_AUTOCOPY
        )
        if not enabled or any(sink.name == _OTP_SINK_NAME for sink in self.sinks):
            return
        try:
            self.sinks.append(
                self._otp_sink_factory(
                    notification_policy=self.notification_policy,
                    session_bus=self._session_bus,
                    amend_message_popup=self._amend_message_popup,
                    resolve_metadata=self._resolve_otp_metadata,
                )
            )
        except Exception:
            log.exception("one-time code clipboard sink failed to init — continuing")

    def _amend_message_popup(self, handle: str, line: str) -> bool:
        """Let the code sink extend the message popup instead of adding one."""
        for sink in self.sinks:
            amend = getattr(sink, "amend_message_popup", None)
            if sink.name == "libnotify" and amend is not None:
                return bool(amend(handle, line))
        return False

    def _remove_sinks(self, name: str) -> None:
        removed = [sink for sink in self.sinks if sink.name == name]
        self.sinks = [sink for sink in self.sinks if sink.name != name]
        for sink in removed:
            close = getattr(sink, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                log.debug("could not close %s sink", name, exc_info=True)

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
        options: dict[str, object] = {}
        if self.contact_photo is not None:
            # Passed only when opted in, so the disabled sink is unchanged.
            options["contact_photo"] = self.contact_photo
        try:
            sink = self._notification_sink_factory(
                defer_mark_read=self.defer_mark_read,
                notification_policy=self.notification_policy,
                contacts_only_notifications=self.contacts_only_notifications,
                on_open_message=self._open_message,
                **options,
                open_target=self.notification_open_target,
                on_open_target=self._open_target,
                on_ancs_action=self.perform_ancs_action,
                ancs_actions_enabled=self.ancs_actions_enabled,
                on_call_action=self.on_call_action,
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

    def ancs_removed(self, notification_id: int) -> None:
        """Retire desktop popups whose iPhone notification was removed."""
        for sink in self.sinks:
            close = getattr(sink, "close_ancs_notification", None)
            if close is None:
                continue
            try:
                close(notification_id)
            except Exception:
                log.exception("sink %s failed to close ANCS popup", sink.name)

    def ancs_actions_reset(self) -> None:
        """Retire every action popup after the ANCS session reset its UIDs."""
        for sink in self.sinks:
            close_all = getattr(sink, "close_all_ancs_notifications", None)
            if close_all is None:
                continue
            try:
                close_all()
            except Exception:
                log.exception("sink %s failed to retire ANCS popups", sink.name)

    def _open_message(self, handle: str, token: str) -> None:
        request_message_activation(handle, token)

    def _open_target(self, target, token: str) -> None:
        request_open_target(target, token)

    def open_notification_click(self, click_id: str, token: str) -> bool:
        """A notification shell ran a mapped popup's argv (see notification_open)."""
        for sink in self.sinks:
            open_click = getattr(sink, "open_click", None)
            if sink.name == "libnotify" and open_click is not None:
                return bool(open_click(click_id, token))
        return False

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

    def message_read(self, handle: str) -> None:
        """Retire pending code copies when the phone or a client reads a message."""
        for sink in self.sinks:
            note_read = getattr(sink, "message_read", None)
            if note_read is not None:
                try:
                    note_read(handle)
                except Exception:
                    log.exception("sink %s failed to observe a message read", sink.name)

    def call(self, event) -> None:
        """Deliver an optional HFP call event to local desktop sinks only.

        Call events are not written to history, and nothing about them is
        broadcast here; the call controller emits the content-free
        CallsChanged invalidation itself.
        """
        for sink in self.sinks:
            handler = getattr(sink, "handle_call", None)
            if handler is None:
                continue
            try:
                handler(event)
            except Exception:
                log.exception("sink %s failed on a call event", sink.name)

    def phone_battery_low(self, percent: int, *, exact: bool = False) -> None:
        """Warn local desktop sinks that the phone's battery is low.

        ``exact`` is false for HFP's 20 % steps. Not persisted and not
        broadcast; GetStatus carries the level.
        """
        for sink in self.sinks:
            handler = getattr(sink, "handle_phone_battery_low", None)
            if handler is None:
                continue
            try:
                handler(percent, exact=exact)
            except Exception:
                log.exception("sink %s failed on a phone battery warning", sink.name)

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

    def missed_calls(self, notices) -> None:
        """Fan newly missed calls out to sinks that can present them.

        Call history is not message history: persistence belongs to the call
        repository, and nothing about a call is published on D-Bus here.
        """
        for sink in self.sinks:
            handler = getattr(sink, "handle_missed_calls", None)
            if handler is None:
                continue
            try:
                handler(list(notices))
            except Exception:
                log.exception("sink %s failed on missed calls", sink.name)

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
