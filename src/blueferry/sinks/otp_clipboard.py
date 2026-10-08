"""Copy one-time codes from newly received messages to the clipboard.

Opt-in through ``BLUEFERRY_OTP_AUTOCOPY``. Only a live MAP push of an
unread incoming message from a sender that is not a saved contact
qualifies: sent messages, listed history, known group conversations,
messages from contacts, and messages without a recent, plausible timestamp
are ignored, and at most a few codes per minute are copied. The code is
never logged, stored, or published on BlueFerry's own D-Bus API. When
``BLUEFERRY_SHOW_NOTIFICATION_CONTENT`` allows message content, the popup
text (and so the desktop notification server) shows it, just as the
message popup shows the message itself.
"""
from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Protocol

from blueferry import config
from blueferry.ancs.events import AncsEvent
from blueferry.events import SmsEvent
from blueferry.notification_policy import NO_NOTIFICATIONS
from blueferry.otp import extract_otp
from blueferry.otp_clipboard import ClipboardTicket, ClipboardWriter
from blueferry.otp_context import OtpContext, OtpMetadata
from blueferry.text_safety import terminal_text

log = logging.getLogger(__name__)

_APP_NAME = "BlueFerry"
_NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
_NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
# A code this old has probably expired or been used; copying it would
# surprise the user by replacing whatever they copied since.
MAX_CODE_AGE = timedelta(minutes=5)
MetadataResolver = Callable[[SmsEvent, Callable[[OtpMetadata | None], None]], None]
# At most this many codes per window; a burst is far more likely spam or a
# bug than a series of logins.
MAX_COPIES_PER_WINDOW = 3
COPY_WINDOW = timedelta(minutes=1)
# Wait briefly before announcing success so a helper that cannot reach the
# display (and exits at once) does not produce a false "copied" popup.
_CONFIRM_DELAY_MS = 400
# ANCS may deliver group context after MAP. Give it the same grace period
# used before acknowledging reads, without delaying ordinary message delivery.
_GROUP_GRACE_MS = 5000
_MAX_SEEN_HANDLES = 64
_X11_FALLBACK_EXCLUDE = frozenset({"wl-copy"})


class Notifier(Protocol):
    def notify(self, summary: str, body: str) -> None: ...

    def close(self) -> None: ...


class DesktopNotifier:
    """Transient popups that follow the notification server's owner.

    Like the libnotify sink, it watches ``NameOwnerChanged`` instead of
    resolving the server for every popup, so a replaced notification daemon
    is picked up and an absent one costs nothing.
    """

    def __init__(self, bus=None) -> None:
        self._bus = bus
        self._interface: Any = None
        self._match: Any = None
        self._available = False
        self._owner_seen = False
        try:
            if self._bus is None:
                from blueferry.bus import get_session_bus

                self._bus = get_session_bus()
            self._match = self._bus.add_signal_receiver(
                self._owner_changed,
                dbus_interface="org.freedesktop.DBus",
                signal_name="NameOwnerChanged",
                bus_name="org.freedesktop.DBus",
                arg0=_NOTIFICATIONS_NAME,
            )
            # Ask asynchronously; a NameOwnerChanged seen meanwhile is newer.
            self._bus.call_async(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                "s",
                (_NOTIFICATIONS_NAME,),
                self._initial_owner,
                self._initial_owner_failed,
            )
        except Exception:
            log.debug("desktop notifications unavailable for one-time codes", exc_info=True)

    def _initial_owner(self, has_owner) -> None:
        if not self._owner_seen:
            self._available = bool(has_owner)

    def _initial_owner_failed(self, error) -> None:
        log.debug("could not query the notification service: %s", type(error).__name__)

    def _owner_changed(self, _name, _old_owner, new_owner) -> None:
        self._owner_seen = True
        self._interface = None
        self._available = bool(new_owner)

    def notify(self, summary: str, body: str) -> None:
        if not self._available or self._bus is None:
            log.debug("no desktop notification service; code copied without popup")
            return
        import dbus

        if self._interface is None:
            self._interface = dbus.Interface(
                self._bus.get_object(_NOTIFICATIONS_NAME, _NOTIFICATIONS_PATH),
                _NOTIFICATIONS_NAME,
            )
        self._interface.Notify(
            _APP_NAME,
            dbus.UInt32(0),
            "edit-paste",
            summary,
            body,
            dbus.Array([], signature="s"),
            dbus.Dictionary(
                {
                    "urgency": dbus.Byte(1),
                    # The popup may contain the code; keep it out of the
                    # notification center's history.
                    "transient": dbus.Boolean(True),
                },
                signature="sv",
            ),
            dbus.Int32(config.NOTIFICATION_TIMEOUT_MS),
            reply_handler=lambda _nid: None,
            error_handler=lambda error: log.debug(
                "one-time code notification failed: %s", type(error).__name__
            ),
        )

    def close(self) -> None:
        match, self._match = self._match, None
        if match is not None:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove notification owner watch", exc_info=True)
        self._interface = None


def notification_text(
    code: str, sender: str, *, show_content: bool, clear_after_s: int
) -> tuple[str, str]:
    """Return the (summary, body) for the "code copied" popup."""
    if show_content:
        summary = f"Code {code} copied"
        body = f"From {sender}. Paste it with Ctrl+V."
    else:
        summary = "Verification code copied"
        body = "Paste it with Ctrl+V."
    if clear_after_s:
        body += f" BlueFerry releases its clipboard copy in {clear_after_s} seconds."
    summary = escape(terminal_text(summary).replace("\n", " "))
    body = escape(terminal_text(body))
    return summary, body


def amendment_text(*, clear_after_s: int) -> str:
    """Return the plain line appended to the message's own popup.

    It never repeats the code: the message popup already shows the text
    when content is allowed, and must not reveal it otherwise.
    """
    line = "Verification code copied to the clipboard."
    if clear_after_s:
        line += f" BlueFerry releases its clipboard copy in {clear_after_s} seconds."
    return line


class OtpClipboardSink:
    name = "otp-clipboard"

    def __init__(
        self,
        *,
        writer: ClipboardWriter,
        notification_policy: Callable[[], str] | None = None,
        notifier: Notifier | None = None,
        schedule_ms: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], object] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        amend_message_popup: Callable[[str, str], bool] | None = None,
        resolve_metadata: MetadataResolver | None = None,
    ) -> None:
        if schedule_ms is None or cancel is None:
            from gi.repository import GLib

            def idle_timeout(delay_ms: int, callback: Callable[[], bool]) -> int:
                # Idle priority lets the helper's child watch (default
                # priority) dispatch first, so a helper that already died is
                # seen as failed rather than running.
                return GLib.timeout_add(
                    delay_ms, callback, priority=GLib.PRIORITY_DEFAULT_IDLE
                )

            schedule_ms = schedule_ms or idle_timeout
            cancel = cancel or GLib.source_remove
        self._writer = writer
        self._notification_policy = notification_policy
        self._notifier = notifier if notifier is not None else DesktopNotifier()
        self._schedule_ms = schedule_ms
        self._cancel = cancel
        self._now = now
        self._amend_message_popup = amend_message_popup
        self._resolve_metadata = resolve_metadata
        self._context = OtpContext(now)
        self._closed = False
        self._request_sequence = 0
        self._last_copied_sequence = 0
        self._recent_copies: deque[datetime] = deque()
        self._recent_attempts: deque[datetime] = deque()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending_confirms: set[int] = set()
        self._pending_checks: set[int] = set()
        self._pending_candidates: dict[int, tuple[SmsEvent, datetime]] = {}
        self._warned_fallback = False
        self._writer.start_probe()
        log.info("one-time code clipboard sink ready")

    def close(self) -> None:
        self._closed = True
        for source in self._pending_confirms | self._pending_checks:
            try:
                self._cancel(source)
            except Exception:
                log.debug("could not cancel a clipboard confirmation", exc_info=True)
        self._pending_confirms.clear()
        self._pending_checks.clear()
        self._pending_candidates.clear()
        self._context.clear()
        self._writer.close()
        self._notifier.close()

    def _is_new_incoming(self, event: SmsEvent) -> bool:
        if getattr(event, "kind", None) != "sms_received":
            return False
        # Only MNS pushes carry a live Message1 path; anything else is a
        # reconstructed or listed record.
        if not getattr(event, "message_path", None):
            return False
        if getattr(event, "is_read", False):
            # Read on the phone already, or a replay of an old message.
            return False
        if getattr(event, "contact_name", None) or getattr(event, "group_key", None):
            # Codes come from services, not from people the user saved or
            # group conversations; there a number next to "code" is chat.
            return False
        handle = str(getattr(event, "handle", "") or "")
        if not handle or handle in self._seen:
            return False
        self._seen[handle] = None
        while len(self._seen) > _MAX_SEEN_HANDLES:
            self._seen.popitem(last=False)
        return True

    def _is_recent(self, timestamp: object) -> bool:
        """Fail closed: only a parsed, zone-aware, plausible time counts."""
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            log.debug("ignoring a message without a usable time for one-time code copy")
            return False
        now = self._now()
        age = now - timestamp
        if age > MAX_CODE_AGE:
            # Content-free: offset-less iPhone timestamps are read in the
            # local zone, so a zone mismatch shows up here.
            log.debug(
                "ignoring a message %d seconds old for one-time code copy",
                int(age.total_seconds()),
            )
            return False
        if age < timedelta(0):
            log.debug(
                "ignoring a message %d seconds in the future for one-time code copy",
                int(-age.total_seconds()),
            )
            return False
        return True

    def _amended(self, handle: str) -> bool:
        """Extend the message's own popup rather than show a second one."""
        amend = self._amend_message_popup
        if amend is None or not handle:
            return False
        try:
            return bool(amend(handle, amendment_text(clear_after_s=self._writer.clear_after_s)))
        except Exception as error:
            log.debug("could not extend the message popup: %s", type(error).__name__)
            return False

    def _within_rate_limit(self) -> bool:
        now = self._now()
        while self._recent_copies and now - self._recent_copies[0] > COPY_WINDOW:
            self._recent_copies.popleft()
        if len(self._recent_copies) >= MAX_COPIES_PER_WINDOW:
            log.warning("not copying a one-time code: too many codes in the last minute")
            return False
        self._recent_copies.append(now)
        return True

    def handle(self, event: SmsEvent) -> None:
        if not self._closed:
            self._context.message(event)
            self._invalidate_candidates()
        if self._closed or not self._is_new_incoming(event):
            return
        code = extract_otp(getattr(event, "body", None))
        if code is None:
            return
        if self._context.is_read(event) or self._context.is_group(event):
            return
        now = self._now()
        while self._recent_attempts and now - self._recent_attempts[0] > COPY_WINDOW:
            self._recent_attempts.popleft()
        if len(self._recent_attempts) >= MAX_COPIES_PER_WINDOW:
            log.debug("one-time code eligibility checks rate limited")
            return
        self._recent_attempts.append(now)
        self._request_sequence += 1
        sequence = self._request_sequence
        self._pending_candidates[sequence] = (event, now + MAX_CODE_AGE)
        source: int | None = None

        def check() -> bool:
            if source is not None:
                self._pending_checks.discard(source)
            self._check_metadata(event, code, sequence)
            return False

        source = self._schedule_ms(_GROUP_GRACE_MS, check)
        self._pending_checks.add(source)

    def handle_ancs(self, event: AncsEvent) -> None:
        if not self._closed:
            self._context.ancs(event)
            self._invalidate_candidates()

    def message_read(self, handle: str) -> None:
        self._context.read(handle)
        self._invalidate_candidates()

    def _invalidate_candidates(self) -> None:
        # Latch read/group evidence while a worker is queued. Expiring or
        # evicting correlation records must never make a blocked code eligible
        # again. At three arrivals per minute, the five-minute lifetime also
        # bounds this pending state even if the phone worker is stalled.
        now = self._now()
        for sequence, (event, deadline) in list(self._pending_candidates.items()):
            if (now > deadline or self._context.is_read(event)
                    or self._context.is_group(event)):
                del self._pending_candidates[sequence]

    def _eligible(self, event: SmsEvent, sequence: int) -> bool:
        return not (
            self._closed or sequence not in self._pending_candidates
            or sequence <= self._last_copied_sequence
            or self._context.is_read(event) or event.contact_name or self._context.is_group(event)
        )

    def _check_metadata(self, event: SmsEvent, code: str, sequence: int) -> None:
        if not self._eligible(event, sequence):
            return
        if self._resolve_metadata is not None:
            try:
                self._resolve_metadata(
                    event,
                    lambda metadata: self._metadata_ready(event, code, sequence, metadata),
                )
            except Exception as error:
                log.debug("one-time code timestamp lookup failed: %s", type(error).__name__)
            return
        self._copy_recent(event, code, sequence, event.timestamp)

    def _metadata_ready(
        self, event: SmsEvent, code: str, sequence: int, metadata: OtpMetadata | None,
    ) -> None:
        if metadata is None or metadata.is_read or not self._eligible(event, sequence):
            return
        self._copy_recent(event, code, sequence, metadata.timestamp)

    def _copy_recent(
        self, event: SmsEvent, code: str, sequence: int, timestamp: datetime | None,
    ) -> None:
        if not self._eligible(event, sequence) or not self._is_recent(timestamp):
            return
        self._writer.prepare(lambda: self._copy_prepared(event, code, sequence, timestamp))

    def _copy_prepared(
        self, event: SmsEvent, code: str, sequence: int, timestamp: datetime | None,
    ) -> None:
        if not self._eligible(event, sequence):
            return
        # The worker may have waited in a queue: check age at copy time, not
        # when the notification arrived. An old lookup cannot replace a newer copy.
        if not self._is_recent(timestamp) or not self._within_rate_limit():
            return
        ticket = self._writer.copy(code)
        if ticket is None:
            return
        self._last_copied_sequence = sequence
        sender = str(getattr(event, "display_sender", "") or "")
        handle = str(getattr(event, "handle", "") or "")
        self._schedule_confirm(
            event, code, sender, handle, ticket, timestamp=timestamp, retried=False,
        )

    def _schedule_confirm(
        self, event: SmsEvent, code: str, sender: str, handle: str, ticket: ClipboardTicket,
        *, timestamp: datetime | None, retried: bool,
    ) -> None:
        source: int | None = None

        def fire() -> bool:
            if source is not None:
                self._pending_confirms.discard(source)
            self._confirm(event, code, sender, handle, ticket, timestamp=timestamp, retried=retried)
            return False

        source = self._schedule_ms(_CONFIRM_DELAY_MS, fire)
        self._pending_confirms.add(source)

    def _confirm(
        self, event: SmsEvent, code: str, sender: str, handle: str, ticket: ClipboardTicket,
        *, timestamp: datetime | None, retried: bool,
    ) -> None:
        state = self._writer.state(ticket)
        if state == "superseded":
            # A newer code (or shutdown) replaced this helper; the newer
            # copy confirms itself.
            return
        if state == "failed":
            if ticket.tool == "wl-copy" and not retried:
                if (self._closed or self._context.is_read(event) or self._context.is_group(event)
                        or not self._is_recent(timestamp)):
                    return
                if not self._warned_fallback:
                    log.warning("wl-copy could not take the clipboard; trying X11 helpers")
                    self._warned_fallback = True
                fallback = self._writer.copy(code, exclude=_X11_FALLBACK_EXCLUDE)
                if fallback is not None:
                    self._schedule_confirm(
                        event, code, sender, handle, fallback, timestamp=timestamp, retried=True,
                    )
                return
            hint_for = getattr(self._writer, "failure_hint", None)
            hint = hint_for(ticket) if hint_for is not None else None
            if hint:
                log.warning(
                    "clipboard helper %s could not take the clipboard: %s", ticket.tool, hint
                )
            else:
                log.warning("clipboard helper %s could not take the clipboard", ticket.tool)
            return
        log.info("copied a one-time code to the clipboard via %s", ticket.tool)
        policy = self._notification_policy
        if policy is not None and str(policy()) == NO_NOTIFICATIONS:
            return
        if self._amended(handle):
            return
        summary, body = notification_text(
            code,
            sender,
            show_content=config.SHOW_NOTIFICATION_CONTENT,
            clear_after_s=self._writer.clear_after_s,
        )
        try:
            self._notifier.notify(summary, body)
        except Exception as error:
            log.debug("one-time code notification failed: %s", type(error).__name__)
