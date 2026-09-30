"""Copy one-time codes from newly received messages to the clipboard.

Opt-in through ``BLUEFERRY_OTP_AUTOCOPY``. Only a live MAP push of an
incoming message qualifies: sent messages, listed or replayed history, and
messages older than a few minutes are ignored. The code is never logged,
stored, or published on BlueFerry's D-Bus API; only the transient desktop
popup may show it, and only when ``BLUEFERRY_SHOW_NOTIFICATION_CONTENT``
allows message content.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Protocol

from blueferry import config
from blueferry.events import SmsEvent
from blueferry.notification_policy import NO_NOTIFICATIONS
from blueferry.otp import extract_otp
from blueferry.otp_clipboard import ClipboardTicket, ClipboardWriter
from blueferry.text_safety import terminal_text

log = logging.getLogger(__name__)

_APP_NAME = "BlueFerry"
_NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
_NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
# A code this old has probably expired or been used; copying it would
# surprise the user by replacing whatever they copied since.
MAX_CODE_AGE = timedelta(minutes=10)
# Wait briefly before announcing success so a helper that cannot reach the
# display (and exits at once) does not produce a false "copied" popup.
_CONFIRM_DELAY_MS = 400
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
            self._available = bool(self._bus.name_has_owner(_NOTIFICATIONS_NAME))
        except Exception:
            log.debug("desktop notifications unavailable for one-time codes", exc_info=True)

    def _owner_changed(self, _name, _old_owner, new_owner) -> None:
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
        body += f" The clipboard is cleared in {clear_after_s} seconds."
    summary = escape(terminal_text(summary).replace("\n", " "))
    body = escape(terminal_text(body))
    return summary, body


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
    ) -> None:
        if schedule_ms is None or cancel is None:
            from gi.repository import GLib

            schedule_ms = schedule_ms or GLib.timeout_add
            cancel = cancel or GLib.source_remove
        self._writer = writer
        self._notification_policy = notification_policy
        self._notifier = notifier if notifier is not None else DesktopNotifier()
        self._schedule_ms = schedule_ms
        self._cancel = cancel
        self._now = now
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending_confirms: set[int] = set()
        self._writer.start_probe()
        log.info("one-time code clipboard sink ready")

    def close(self) -> None:
        for source in list(self._pending_confirms):
            try:
                self._cancel(source)
            except Exception:
                log.debug("could not cancel a clipboard confirmation", exc_info=True)
        self._pending_confirms.clear()
        self._writer.close()
        self._notifier.close()

    def _is_new_incoming(self, event: SmsEvent) -> bool:
        if getattr(event, "kind", None) != "sms_received":
            return False
        # Only MNS pushes carry a live Message1 path; anything else is a
        # reconstructed or listed record.
        if not getattr(event, "message_path", None):
            return False
        handle = str(getattr(event, "handle", "") or "")
        if not handle or handle in self._seen:
            return False
        timestamp = getattr(event, "timestamp", None)
        if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
            age = self._now() - timestamp
            if age > MAX_CODE_AGE:
                # Content-free: offset-less iPhone timestamps are read in the
                # local zone, so a zone mismatch shows up here.
                log.debug(
                    "ignoring a message %d seconds old for one-time code copy",
                    int(age.total_seconds()),
                )
                return False
        self._seen[handle] = None
        while len(self._seen) > _MAX_SEEN_HANDLES:
            self._seen.popitem(last=False)
        return True

    def handle(self, event: SmsEvent) -> None:
        if not self._is_new_incoming(event):
            return
        code = extract_otp(getattr(event, "body", None))
        if code is None:
            return
        ticket = self._writer.copy(code)
        if ticket is None:
            return
        sender = str(getattr(event, "display_sender", "") or "")
        self._schedule_confirm(code, sender, ticket, retried=False)

    def _schedule_confirm(
        self, code: str, sender: str, ticket: ClipboardTicket, *, retried: bool
    ) -> None:
        source: int | None = None

        def fire() -> bool:
            if source is not None:
                self._pending_confirms.discard(source)
            self._confirm(code, sender, ticket, retried=retried)
            return False

        source = self._schedule_ms(_CONFIRM_DELAY_MS, fire)
        self._pending_confirms.add(source)

    def _confirm(self, code: str, sender: str, ticket: ClipboardTicket, *, retried: bool) -> None:
        state = self._writer.state(ticket)
        if state == "superseded":
            # A newer code (or shutdown) replaced this helper; the newer
            # copy confirms itself.
            return
        if state == "failed":
            if ticket.tool == "wl-copy" and not retried:
                log.warning("wl-copy could not take the clipboard; trying X11 helpers")
                fallback = self._writer.copy(code, exclude=_X11_FALLBACK_EXCLUDE)
                if fallback is not None:
                    self._schedule_confirm(code, sender, fallback, retried=True)
                return
            log.warning("clipboard helper %s could not take the clipboard", ticket.tool)
            return
        log.info("copied a one-time code to the clipboard via %s", ticket.tool)
        policy = self._notification_policy
        if policy is not None and str(policy()) == NO_NOTIFICATIONS:
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
