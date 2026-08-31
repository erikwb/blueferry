"""Desktop notification sink via org.freedesktop.Notifications.

Body format: title = display sender (contact name or phone number),
             body  = SMS text (truncated at ~280 chars to avoid huge popups).

Popups request a finite lifetime. If the user explicitly dismisses an SMS
popup before it expires, we mark that message read on the iPhone. If the
iPhone marks it read while the popup is visible, we close it early.

Read-state sync:
  Linux dismiss → MAP Message1.Properties.Set(Read=true)  → iPhone marks read
  iPhone reads  → MAP PropertiesChanged(Read=true)         → we close popup

Empirical on iOS 26.5: both directions work. iPhone propagates Read=true
back over MAP within a few seconds of opening the Messages app.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Protocol

import dbus
import dbus.exceptions

from blueferry import config
from blueferry.ancs.events import AncsEvent
from blueferry.bus import get_session_bus, obex
from blueferry.events import SmsEvent
from blueferry.limits import MAX_DESKTOP_MESSAGE_TRACKERS
from blueferry.notification_policy import (
    ALL_NOTIFICATIONS,
    DEFAULT_NOTIFICATION_POLICY,
    NO_NOTIFICATIONS,
)
from blueferry.text_safety import terminal_text


class _SignalMatch(Protocol):
    def remove(self) -> object: ...

log = logging.getLogger(__name__)

_APP_NAME = "BlueFerry"
_BODY_LIMIT = 280
_MESSAGE_EXPIRE_MS = config.NOTIFICATION_TIMEOUT_MS
# ANCS mirrors ordinary iPhone app/system notifications. Unlike MAP messages,
# they have no desktop-to-phone read-state path, so keeping every popup around
# indefinitely only creates notification-center clutter.
_ANCS_EXPIRE_MS = config.NOTIFICATION_TIMEOUT_MS

# NotificationClosed reason codes (org.freedesktop.Notifications spec):
#   1 = expired (timeout)
#   2 = dismissed by user
#   3 = CloseNotification() called programmatically (e.g. by us on iPhone-read)
#   4 = undefined / reserved
#
# We mark-read only on dismissed-by-user. Reason 3 = we're already closing
# because the iPhone marked it read (so we'd be in a write-self-write loop).
# Reason 1 is the normal finite-timeout path and must not mark the phone read.
_REASON_DISMISSED = 2


def _mark_message_read(message_path: str) -> None:
    obex(message_path, "org.freedesktop.DBus.Properties").Set(
        "org.bluez.obex.Message1", "Read", dbus.Boolean(True), timeout=10.0
    )


class LibnotifySink:
    name = "libnotify"
    # New sinks fail closed in EventDispatcher. This one accepts system ANCS
    # only to create an immediate transient popup; it retains no event data.
    accepts_system_ancs = True

    def __init__(
        self,
        *,
        submit_obex,
        notification_policy=None,
        contacts_only_notifications=None,
        on_open_message=None,
    ) -> None:
        self._submit_obex = submit_obex
        self._notification_policy = notification_policy
        self._contacts_only_notifications = contacts_only_notifications
        self._on_open_message = on_open_message
        self._notif = dbus.Interface(
            get_session_bus().get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
            ),
            "org.freedesktop.Notifications",
        )
        # notification_id (uint32 from Notify) -> Message1 DBus path
        self._pending: dict[int, str] = {}
        # notification_id -> opaque MAP handle.  Handles let clients locate
        # the message in their private thread snapshot without broadcasting a
        # phone number or message body on the session bus.
        self._open_messages: dict[int, str] = {}
        # notification_id -> SignalMatch for the per-Message1 PropertiesChanged sub
        self._msg_subs: dict[int, _SignalMatch] = {}

        # Listen for any of our notifications closing (dismissed, expired,
        # or programmatically closed).
        self._match = self._notif.connect_to_signal(
            "NotificationClosed", self._on_closed,
        )
        self._action_match = self._notif.connect_to_signal(
            "ActionInvoked", self._on_action,
        )
        log.info("libnotify sink ready (expiring + bidirectional read-sync)")

    def close(self) -> None:
        """Release signal watches before a notification-daemon replacement."""
        for attribute in ("_match", "_action_match"):
            match = getattr(self, attribute, None)
            if match is not None:
                try:
                    match.remove()
                except Exception:
                    log.debug("could not remove libnotify signal watch", exc_info=True)
                setattr(self, attribute, None)
        for subscription in self._msg_subs.values():
            try:
                subscription.remove()
            except Exception:
                log.debug("could not remove message read-state watch", exc_info=True)
        self._msg_subs.clear()
        self._pending.clear()
        self._open_messages.clear()

    def _policy(self) -> str:
        provider = getattr(self, "_notification_policy", None)
        return (
            str(provider())
            if provider is not None
            else DEFAULT_NOTIFICATION_POLICY
        )

    def _contacts_only(self) -> bool:
        provider = getattr(self, "_contacts_only_notifications", None)
        return bool(provider()) if provider is not None else False

    def handle(self, event: SmsEvent) -> None:
        # Don't pop a desktop notification for a message we ourselves sent.
        if event.kind == "sms_sent":
            return
        if self._policy() == NO_NOTIFICATIONS:
            return
        if self._contacts_only() and not getattr(event, "contact_name", None):
            return
        title = f"\U0001f4ac {event.display_sender}"
        body = (event.body or "").strip()
        if not config.SHOW_NOTIFICATION_CONTENT:
            body = "New message"
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT - 1] + "…"
        # The freedesktop body field accepts markup. Message text is remote,
        # untrusted input, so escape it before handing it to the shell.
        title = escape(terminal_text(title).replace("\n", " "))
        body = escape(terminal_text(body))
        try:
            # A deliberate dismissal is propagated as mark-read. Expiration
            # is reason=1 and therefore leaves the iPhone's read state alone.
            handle = str(getattr(event, "handle", "") or "")
            actions = ["default", "Open conversation"] if handle else []
            nid = int(self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "phone-symbolic",
                title,
                body,
                dbus.Array(actions, signature="s"),
                dbus.Dictionary({"urgency": dbus.Byte(1)}, signature="sv"),
                dbus.Int32(_MESSAGE_EXPIRE_MS),
            ))
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify failed: %s", e.get_dbus_name())
            return

        if handle:
            if not hasattr(self, "_open_messages"):
                self._open_messages = {}
            self._open_messages[nid] = handle

        if event.message_path:
            self._pending[nid] = event.message_path
            # Subscribe to PropertiesChanged on this specific Message1 path
            # so we get notified if iOS marks it read.
            try:
                self._msg_subs[nid] = get_session_bus().add_signal_receiver(
                    lambda iface, changed, _inv, nid=nid:
                        self._on_msg_props(nid, iface, changed),
                    dbus_interface="org.freedesktop.DBus.Properties",
                    signal_name="PropertiesChanged",
                    bus_name="org.bluez.obex",
                    path=event.message_path,
                )
            except dbus.exceptions.DBusException as error:
                self._pending.pop(nid, None)
                log.warning(
                    "could not watch message read state: %s",
                    error.get_dbus_name(),
                )
        self._prune_trackers()

    def _prune_trackers(self) -> None:
        """Bound read-state subscriptions if close signals never arrive."""
        open_messages = getattr(self, "_open_messages", {})
        while len(set(self._pending) | set(open_messages)) > MAX_DESKTOP_MESSAGE_TRACKERS:
            tracked = open_messages or self._pending
            oldest = next(iter(tracked))
            self._pending.pop(oldest, None)
            open_messages.pop(oldest, None)
            subscription = self._msg_subs.pop(oldest, None)
            if subscription is not None:
                try:
                    subscription.remove()
                except Exception:
                    log.debug("could not remove stale message watch", exc_info=True)
            try:
                self._notif.CloseNotification(dbus.UInt32(oldest))
            except dbus.exceptions.DBusException:
                log.debug("could not close stale desktop notification", exc_info=True)

    # ---- ANCS events (per-app notifications) ----------------------------

    def handle_ancs(self, event: AncsEvent) -> None:
        if self._policy() != ALL_NOTIFICATIONS:
            return
        # Messages already arrive through MAP. The ANCS copy is retained for
        # group metadata but never creates a second desktop popup.
        if event.app_id == "com.apple.MobileSMS":
            return
        # Title: "📱 AppName" or "📱 com.bundle.id" if no name yet
        app = event.app_name or event.app_id or "Notification"
        title = f"\U0001f4f1 {app}"
        # Body: prefer Title field for headline, then Message
        body_parts = [p for p in (event.title, event.body) if p]
        body = " — ".join(body_parts) if body_parts else ""
        if not config.SHOW_NOTIFICATION_CONTENT:
            body = "New iPhone notification"
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT - 1] + "…"
        title = escape(terminal_text(title).replace("\n", " "))
        body = escape(terminal_text(body))
        try:
            # No mark-read sync exists for ANCS, so use a normal finite popup
            # lifetime. The event remains available in private SQLite history.
            self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "phone-symbolic",
                title,
                body,
                dbus.Array([], signature="s"),
                dbus.Dictionary({
                    "urgency": dbus.Byte(1),
                    # Plasma can retain an expired notification in history.
                    # ANCS events already live in BlueFerry's own feed, so
                    # explicitly bypass desktop notification persistence.
                    "transient": dbus.Boolean(True),
                }, signature="sv"),
                dbus.Int32(_ANCS_EXPIRE_MS),
            )
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify (ANCS) failed: %s", e.get_dbus_name())

    # ---- iPhone marks read → close our popup ----------------------------

    def _on_msg_props(self, nid: int, iface: str, changed) -> None:
        if iface != "org.bluez.obex.Message1":
            return
        # Look for Read going True. Some BlueZ versions send Status instead.
        read_now = (
            bool(changed.get("Read", False))
            or str(changed.get("Status", "")).lower() in ("read", "complete")
        )
        if not read_now:
            return
        if nid not in self._pending:
            return  # already closed/handled
        try:
            self._notif.CloseNotification(dbus.UInt32(nid))
            log.info("iPhone marked message read — closed popup %d", nid)
        except dbus.exceptions.DBusException as e:
            log.debug("CloseNotification(%d): %s", nid, e.get_dbus_name())
        # _on_closed will clean up the dict + signal match (reason=3)

    # ---- Linux user dismisses → mark-read on iPhone ----------------------

    def _on_action(self, nid, action) -> None:
        """Route a notification click to every currently running client."""
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            return
        if str(action) != "default":
            return
        handle = getattr(self, "_open_messages", {}).get(nid_i)
        callback = getattr(self, "_on_open_message", None)
        if handle and callback is not None:
            callback(handle)

    def _on_closed(self, nid, reason) -> None:
        try:
            nid_i = int(nid)
            reason_i = int(reason)
        except (TypeError, ValueError):
            return

        getattr(self, "_open_messages", {}).pop(nid_i, None)
        message_path = self._pending.pop(nid_i, None)

        # Always remove the per-message subscription, no matter the reason
        sub = self._msg_subs.pop(nid_i, None)
        if sub is not None:
            try:
                sub.remove()
            except Exception:
                log.debug("could not remove message read-state watch", exc_info=True)

        if message_path is None:
            return

        # Only propagate read-state to iPhone when the human actively
        # dismissed (reason=2). Don't loop on programmatic close (reason=3,
        # which is fired when we closed it ourselves because iPhone already
        # marked it read).
        if reason_i != _REASON_DISMISSED:
            return
        def succeeded(_result) -> None:
            log.info("marked %s as read on iPhone (user dismissed popup)",
                     message_path.rsplit("/", 1)[-1])

        def failed(error) -> None:
            log.debug("mark-read failed for %s: %s", message_path, error)

        try:
            self._submit_obex(
                lambda: _mark_message_read(message_path),
                on_success=succeeded,
                on_error=failed,
            )
        except Exception as error:
            failed(error)
