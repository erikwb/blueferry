"""Copy one-time codes from newly received messages to the clipboard.

Opt-in through ``BLUEFERRY_OTP_AUTOCOPY``. Only a live MAP push of an
incoming message qualifies: sent messages, listed or replayed history, and
messages older than a few minutes are ignored. The code is never logged,
stored, or sent over D-Bus; only the desktop notification may show it, and
only when ``BLUEFERRY_SHOW_NOTIFICATION_CONTENT`` allows message content.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from html import escape

from blueferry import config
from blueferry.events import SmsEvent
from blueferry.notification_policy import NO_NOTIFICATIONS
from blueferry.otp import extract_otp
from blueferry.otp_clipboard import ClipboardWriter
from blueferry.text_safety import terminal_text

log = logging.getLogger(__name__)

_APP_NAME = "BlueFerry"
# A code this old has probably expired or been used; copying it would
# surprise the user by replacing whatever they copied since.
MAX_CODE_AGE = timedelta(minutes=10)
# Wait briefly before announcing success so a helper that cannot reach the
# display (and exits at once) does not produce a false "copied" popup.
_CONFIRM_DELAY_MS = 400
_MAX_SEEN_HANDLES = 64

Notify = Callable[[str, str], None]


def _desktop_notify(summary: str, body: str) -> None:
    """Show a transient popup without blocking the GLib main loop."""
    import dbus

    from blueferry.bus import get_session_bus

    interface = dbus.Interface(
        get_session_bus().get_object(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications"
        ),
        "org.freedesktop.Notifications",
    )
    interface.Notify(
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
                "category": "transfer.complete",
            },
            signature="sv",
        ),
        dbus.Int32(config.NOTIFICATION_TIMEOUT_MS),
        reply_handler=lambda _nid: None,
        error_handler=lambda error: log.debug(
            "one-time code notification failed: %s", type(error).__name__
        ),
    )


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
        notify: Notify = _desktop_notify,
        schedule_ms: Callable[[int, Callable[[], bool]], int] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if schedule_ms is None:
            from gi.repository import GLib

            schedule_ms = GLib.timeout_add
        self._writer = writer
        self._notification_policy = notification_policy
        self._notify = notify
        self._schedule_ms = schedule_ms
        self._now = now
        self._seen: OrderedDict[str, None] = OrderedDict()
        log.info("one-time code clipboard sink ready")

    def close(self) -> None:
        self._writer.release()

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
            if self._now() - timestamp > MAX_CODE_AGE:
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
        tool = self._writer.copy(code)
        if tool is None:
            return
        sender = str(getattr(event, "display_sender", "") or "")
        self._schedule_ms(_CONFIRM_DELAY_MS, lambda: self._confirm(code, sender, tool))

    def _confirm(self, code: str, sender: str, tool: str) -> bool:
        if self._writer.helper_failed():
            log.warning("clipboard helper %s could not take the clipboard", tool)
            return False
        log.info("copied a one-time code to the clipboard via %s", tool)
        policy = self._notification_policy
        if policy is not None and str(policy()) == NO_NOTIFICATIONS:
            return False
        summary, body = notification_text(
            code,
            sender,
            show_content=config.SHOW_NOTIFICATION_CONTENT,
            clear_after_s=self._writer.clear_after_s,
        )
        try:
            self._notify(summary, body)
        except Exception as error:
            log.debug("one-time code notification failed: %s", type(error).__name__)
        return False
