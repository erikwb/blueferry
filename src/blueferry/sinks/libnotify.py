"""Desktop notification sink via org.freedesktop.Notifications.

Body format: title = display sender (contact name or phone number),
             body  = SMS text (truncated at ~280 chars to avoid huge popups).

Popups request a finite lifetime. If the user explicitly dismisses an SMS
popup before it expires, we defer marking that message read on the iPhone so
ANCS has time to deliver its group metadata (unless
BLUEFERRY_MARK_READ_ON_DISMISS is false). If the iPhone marks it read while
the popup is visible, we close it early.

Read-state sync:
  Linux dismiss → MAP Message1.Properties.Set(Read=true)  → iPhone marks read
  iPhone reads  → MAP PropertiesChanged(Read=true)         → we close popup

Empirical on iOS 26.5: both directions work. iPhone propagates Read=true
back over MAP within a few seconds of opening the Messages app.

ANCS notification actions (opt-in, BLUEFERRY_ANCS_ACTIONS):
  iPhone offers Accept/Decline/Clear/... → popup action buttons
  user clicks one button                 → PerformNotificationAction
Dismissing or expiring an ANCS popup never touches the iPhone, and nothing is
invoked without an explicit click on the labelled button.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from html import escape
from typing import Protocol

import dbus
import dbus.exceptions

from blueferry import config
from blueferry.ancs.constants import (
    ANCS_MESSAGE_MAX_BYTES,
    ANCS_SUBTITLE_MAX_BYTES,
    MESSAGES_APP_ID,
)
from blueferry.ancs.events import AncsEvent
from blueferry.bus import get_session_bus
from blueferry.calls.model import CallEvent
from blueferry.client_activation import activation_argv, select_client
from blueferry.events import SmsEvent
from blueferry.limits import (
    MAX_ANCS_ACTION_POPUPS,
    MAX_DESKTOP_MESSAGE_TRACKERS,
    MAX_NOTIFICATION_CLICK_TRACKERS,
)
from blueferry.notification_open import MAX_CLICK_ID_CHARS, click_argv
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
# Long bodies stay readable: Plasma shows a few lines and expands on click.
# Large enough for the full subtitle and message BlueFerry requests over ANCS.
_BODY_LIMIT = ANCS_SUBTITLE_MAX_BYTES + 1 + ANCS_MESSAGE_MAX_BYTES
_MESSAGE_EXPIRE_MS = config.NOTIFICATION_TIMEOUT_MS
# ANCS mirrors ordinary iPhone app/system notifications. Unlike MAP messages,
# they have no desktop-to-phone read-state path, so keeping every popup around
# indefinitely only creates notification-center clutter.
_ANCS_EXPIRE_MS = config.NOTIFICATION_TIMEOUT_MS
# Popups with iPhone action buttons (a ringing call, an invitation) need to
# stay long enough to be answered.
_ANCS_ACTION_EXPIRE_MS = config.ANCS_ACTION_TIMEOUT_MS

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
# Minimum spacing between two launches of the same notification target.
_OPEN_TARGET_INTERVAL_S = 1.0
# How long a click ID stays usable after the user dismissed its popup. Omarchy
# runs the popup's argv and dismisses the popup at once, so the close arrives
# before the helper has started and called OpenNotificationClick.
_DISMISSED_CLICK_GRACE_S = 10.0

# Notification action keys for ANCS actions. They never collide with the
# message popup's "default" action, so a click on the popup body cannot run an
# iPhone action.
_ANCS_POSITIVE_ACTION = "ancs-positive"
_ANCS_NEGATIVE_ACTION = "ancs-negative"
_ANCS_RETRY_ACTION = "ancs-retry"
# Action labels are plain strings, but some notification servers interpret
# markup in them. Remove only things shaped like real tags (``<b>``,
# ``</i>``, ``<a href=...>``), then drop the remaining markup-significant
# characters instead of escaping, so a server that does not parse markup shows
# no literal entities and "Reply <3 > now" keeps its words.
_LABEL_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_LABEL_MARKUP_RE = re.compile(r"[<>&]")
# Results worth a "Retry" button: the UID is still valid on the phone.
_ANCS_RETRYABLE = frozenset({"busy", "failed"})
_ANCS_ACTION_FEEDBACK = {
    "unavailable": "The notification is no longer available on the iPhone.",
    "disconnected": "The iPhone is not connected.",
    "busy": "Another iPhone action is still in progress.",
    "rejected": "The iPhone could not complete the action.",
    "unsupported": "The iPhone does not support this action.",
    "failed": "The action could not be sent to the iPhone.",
}

# Incoming-call popups stay until the call stops ringing; the sink closes
# them itself when the call is answered, declined, or disappears.
_CALL_EXPIRE_MS = 0
_CALL_ACTIONS = ("answer", "decline")


def _notification_hints(handle: str) -> dict[str, object]:
    hints: dict[str, object] = {"urgency": dbus.Byte(1)}
    if not handle:
        return hints
    client = select_client(get_session_bus().list_names())
    if client is not None:
        hints["desktop-entry"] = client.desktop_id
    # Omarchy's notification shell prefers this JSON argv over a live
    # libnotify action, and it survives a shell restart.
    hints["omarchy-exec-argv"] = json.dumps(activation_argv(handle))
    return hints


class LibnotifySink:
    name = "libnotify"
    # New sinks fail closed in EventDispatcher. This one accepts system ANCS
    # only to create an immediate transient popup; it retains no event data.
    accepts_system_ancs = True

    def __init__(
        self,
        *,
        defer_mark_read: Callable[[str], None],
        notification_policy=None,
        contacts_only_notifications=None,
        on_open_message=None,
        open_target=None,
        on_open_target=None,
        on_ancs_action=None,
        ancs_actions_enabled=None,
        on_call_action=None,
    ) -> None:
        self._defer_mark_read = defer_mark_read
        # (call_id, "answer" | "decline") from an incoming-call popup button.
        self._on_call_action = on_call_action
        # notification_id <-> call_id for ringing-call popups.
        self._call_notifications: dict[int, str] = {}
        self._call_popups: dict[str, int] = {}
        self._notification_policy = notification_policy
        self._contacts_only_notifications = contacts_only_notifications
        self._on_open_message = on_open_message
        # Resolves an ANCS bundle ID to the user's click rule (or None).
        self._open_target = open_target
        self._on_open_target = on_open_target
        # (uid, positive, token, on_result) -> queued; None disables actions.
        self._on_ancs_action = on_ancs_action
        self._ancs_actions_provider = ancs_actions_enabled
        # desktop notification id -> (ANCS uid, offer token) for popups with
        # iPhone actions, and for "not completed" popups with a Retry button
        # -> (uid, positive, token).
        self._ancs_actions: dict[int, tuple[int, int]] = {}
        self._ancs_retries: dict[int, tuple[int, bool, int]] = {}
        # Buttons are offered only once the server confirmed the "actions"
        # capability; a server that draws no buttons gets the plain popup.
        self._server_actions = False
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
        self._activation_tokens: dict[int, str] = {}
        # notification_id -> ANCS bundle ID with a configured click rule. The
        # rule itself is looked up again on click so a removed rule is final.
        self._open_apps: dict[int, str] = {}
        # Random per-popup click ID -> notification_id. Shells that run an
        # argv instead of invoking the action (Omarchy) hand this opaque ID
        # back through the daemon, so the same click path applies.
        self._click_ids: dict[str, int] = {}
        # Click ID -> (bundle ID, monotonic deadline) for popups the user
        # just dismissed; see _DISMISSED_CLICK_GRACE_S.
        self._dismissed_clicks: dict[str, tuple[str, float]] = {}
        # target -> monotonic time of its last launch (per-target throttle)
        self._recent_open_targets: dict[object, float] = {}
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
        self._token_match = self._notif.connect_to_signal(
            "ActivationToken", self._on_activation_token,
        )
        self._query_capabilities()
        log.info("libnotify sink ready (expiring + bidirectional read-sync)")

    def _query_capabilities(self) -> None:
        def received(capabilities) -> None:
            self._server_actions = "actions" in {str(item) for item in capabilities}
            log.debug(
                "notification server %s action buttons",
                "supports" if self._server_actions else "does not support",
            )

        def failed(error) -> None:
            name = getattr(error, "get_dbus_name", lambda: None)()
            log.debug("GetCapabilities failed: %s", name or type(error).__name__)

        try:
            self._notif.GetCapabilities(reply_handler=received, error_handler=failed)
        except dbus.exceptions.DBusException as error:
            failed(error)

    def close(self) -> None:
        """Release signal watches before a notification-daemon replacement."""
        for attribute in ("_match", "_action_match", "_token_match"):
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
        getattr(self, "_open_apps", {}).clear()
        getattr(self, "_click_ids", {}).clear()
        getattr(self, "_dismissed_clicks", {}).clear()
        getattr(self, "_activation_tokens", {}).clear()
        # The popups themselves are retired by close_all_ancs_notifications()
        # while the server is still ours; after an owner change the old
        # server's popups are gone, and their ids may name someone else's.
        getattr(self, "_ancs_actions", {}).clear()
        getattr(self, "_ancs_retries", {}).clear()
        getattr(self, "_call_notifications", {}).clear()
        getattr(self, "_call_popups", {}).clear()

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
                dbus.Dictionary(_notification_hints(handle), signature="sv"),
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
        """Bound read-state and click trackers if close signals never arrive.

        Message popups and mapped app popups have separate budgets: a burst
        of iPhone app notifications must never evict a message popup and
        with it the dismiss-to-read sync, as it could not before click rules.
        """
        open_messages = getattr(self, "_open_messages", {})
        open_apps = getattr(self, "_open_apps", {})
        while True:
            if len(set(self._pending) | set(open_messages)) > MAX_DESKTOP_MESSAGE_TRACKERS:
                oldest = next(iter(open_messages or self._pending))
            elif len(open_apps) > MAX_NOTIFICATION_CLICK_TRACKERS:
                oldest = next(iter(open_apps))
            else:
                return
            self._pending.pop(oldest, None)
            open_messages.pop(oldest, None)
            open_apps.pop(oldest, None)
            self._forget_click_id(oldest)
            getattr(self, "_activation_tokens", {}).pop(oldest, None)
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
        if event.app_id == MESSAGES_APP_ID:
            return
        # Title: "📱 AppName" or "📱 com.bundle.id" if no name yet
        app = event.app_name or event.app_id or "Notification"
        # Mirror the iPhone's layout: the notification's own title next to the
        # app name, then subtitle and message on separate lines.
        title = f"\U0001f4f1 {app}"
        if config.SHOW_NOTIFICATION_CONTENT:
            # Skip a title that only repeats the app name ("github" under
            # "GitHub") or is blank, so no dangling separator is left.
            headline = event.title.strip()
            if headline and headline.casefold() != app.strip().casefold():
                title = f"{title} \u00b7 {headline}"
            body_parts = [p.strip() for p in (event.subtitle, event.body) if p.strip()]
            body = "\n".join(body_parts)
        else:
            body = "New iPhone notification"
        if len(body) > _BODY_LIMIT:
            body = body[:_BODY_LIMIT - 1] + "…"
        title = escape(terminal_text(title).replace("\n", " "))
        body = escape(terminal_text(body))
        buttons = (
            self._ancs_action_buttons(event)
            if getattr(event, "has_actions", False)
            else []
        )
        app_id = event.app_id if isinstance(event.app_id, str) else ""
        # Only a user-configured rule adds a click action; without one the
        # popup stays exactly as before. The action label is fixed text.
        open_target = self._resolve_open_target(app_id)
        clickable = open_target is not None
        # With iPhone action buttons, "Open" takes the place of their no-op
        # default action; the buttons themselves stay.
        actions = ["default", "Open", *buttons[2:]] if clickable else buttons
        hints: dict[str, object] = {
            "urgency": dbus.Byte(1),
            # Plasma can retain an expired notification in history.
            # ANCS events already live in BlueFerry's own feed, so
            # explicitly bypass desktop notification persistence.
            "transient": dbus.Boolean(True),
        }
        click_id = ""
        if clickable:
            # Omarchy's shell runs this argv instead of invoking the action.
            # It carries only a random per-popup ID, never the target or the
            # bundle ID; the helper hands it back to the daemon, which applies
            # the current rule, the throttle and the one-shot tracker exactly
            # as for a live click. A restored toast's ID is unknown once the
            # popup has been closed for a few seconds or the daemon restarted,
            # so it opens nothing.
            click_id = secrets.token_urlsafe(18)
            hints["omarchy-exec-argv"] = json.dumps(click_argv(click_id))
        try:
            # No mark-read sync exists for ANCS, so use a normal finite popup
            # lifetime. The event remains available in private SQLite history.
            nid = self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "phone-symbolic",
                title,
                body,
                dbus.Array(actions, signature="s"),
                dbus.Dictionary(hints, signature="sv"),
                dbus.Int32(_ANCS_ACTION_EXPIRE_MS if buttons else _ANCS_EXPIRE_MS),
            )
        except dbus.exceptions.DBusException as e:
            log.error("libnotify Notify (ANCS) failed: %s", e.get_dbus_name())
            return
        if buttons:
            self._track_ancs_actions(
                int(nid),
                int(event.notification_id),
                int(getattr(event, "action_token", 0)),
            )
        if clickable:
            if not hasattr(self, "_open_apps"):
                self._open_apps = {}
            self._open_apps[int(nid)] = app_id
            if not hasattr(self, "_click_ids"):
                self._click_ids = {}
            self._click_ids[click_id] = int(nid)
            self._prune_trackers()

    def _resolve_open_target(self, app_id: str):
        provider = getattr(self, "_open_target", None)
        if provider is None or not app_id:
            return None
        try:
            return provider(app_id)
        except Exception:
            log.debug("could not resolve a notification click rule", exc_info=True)
            return None

    def _ancs_actions_enabled(self) -> bool:
        # Labels are app-defined ("Pay CHF 50 to Bob"), so they count as
        # notification content; the provider also checks
        # BLUEFERRY_SHOW_NOTIFICATION_CONTENT.
        provider = getattr(self, "_ancs_actions_provider", None)
        try:
            enabled = bool(provider()) if provider is not None else False
        except Exception:
            log.exception("ANCS actions setting provider raised")
            return False
        return enabled and getattr(self, "_on_ancs_action", None) is not None

    def _ancs_action_buttons(self, event: AncsEvent) -> list[str]:
        """Return freedesktop action pairs for the iPhone-offered actions."""
        if not self._ancs_actions_enabled() or not getattr(
            self, "_server_actions", False
        ):
            return []
        actions: list[str] = []
        for key, label in (
            (_ANCS_POSITIVE_ACTION, getattr(event, "positive_action_label", "")),
            (_ANCS_NEGATIVE_ACTION, getattr(event, "negative_action_label", "")),
        ):
            # Action labels are plain text in the spec, but some servers
            # render them loosely; apply the same display sanitizing.
            text = terminal_text(str(label or "")).replace("\n", " ")
            text = _LABEL_MARKUP_RE.sub("", _LABEL_TAG_RE.sub("", text))
            text = " ".join(text.split())
            if text:
                actions += [key, text]
        if actions:
            # An explicit no-op default action: servers that map a click on
            # the popup body to the only action (or the first one) would
            # otherwise run "Decline" or "Delete" on a plain body click.
            actions = ["default", "", *actions]
        return actions

    def _track_ancs_actions(self, nid: int, uid: int, token: int) -> None:
        tracked = getattr(self, "_ancs_actions", None)
        if tracked is None:
            tracked = self._ancs_actions = {}
        tracked.pop(nid, None)
        tracked[nid] = (uid, token)
        while len(tracked) > MAX_ANCS_ACTION_POPUPS:
            tracked.pop(next(iter(tracked)))

    def close_ancs_notification(self, uid: int) -> None:
        """Close a popup whose iPhone notification was removed on the phone."""
        for table in (
            getattr(self, "_ancs_actions", {}),
            getattr(self, "_ancs_retries", {}),
        ):
            for nid in [nid for nid, value in table.items() if value[0] == uid]:
                table.pop(nid, None)
                self._close_async(nid)

    def close_all_ancs_notifications(self) -> None:
        """Retire every action popup when the ANCS session (and UIDs) reset.

        A new session may reuse a UID, so an old button must never stay
        wired to it.
        """
        stale: list[int] = []
        for table in (
            getattr(self, "_ancs_actions", {}),
            getattr(self, "_ancs_retries", {}),
        ):
            stale += list(table)
            table.clear()
        for nid in stale:
            self._close_async(nid)

    def _close_async(self, nid: int) -> None:
        def failed(error) -> None:
            name = getattr(error, "get_dbus_name", lambda: None)()
            log.debug("CloseNotification(%d): %s", nid, name or type(error).__name__)

        try:
            self._notif.CloseNotification(
                dbus.UInt32(nid),
                reply_handler=lambda *_args: None,
                error_handler=failed,
            )
        except dbus.exceptions.DBusException as error:
            failed(error)

    def _invoke_ancs_action(self, nid: int, action: str) -> None:
        # Pop first: a popup's buttons are single-use even if the server
        # delivers ActionInvoked twice.
        if action == _ANCS_RETRY_ACTION:
            retry = getattr(self, "_ancs_retries", {}).pop(nid, None)
            if retry is None:
                return
            uid, positive, token = retry
        else:
            offer = getattr(self, "_ancs_actions", {}).pop(nid, None)
            if offer is None:
                return
            uid, token = offer
            positive = action == _ANCS_POSITIVE_ACTION
        callback = getattr(self, "_on_ancs_action", None)
        if callback is None or not self._ancs_actions_enabled():
            return

        def result(outcome: str) -> None:
            self._ancs_action_result(outcome, uid, positive, token)

        try:
            callback(uid, positive, token, result)
        except Exception:
            log.exception("ANCS action callback raised")

    def _ancs_action_result(
        self, result: str, uid: int, positive: bool, token: int
    ) -> None:
        """Tell the user when a clicked iPhone action did not go through."""
        message = _ANCS_ACTION_FEEDBACK.get(result)
        if message is None:
            return
        retryable = result in _ANCS_RETRYABLE and getattr(
            self, "_server_actions", False
        )
        actions = ["default", "", _ANCS_RETRY_ACTION, "Retry"] if retryable else []
        try:
            nid = self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "phone-symbolic",
                "\U0001f4f1 iPhone action not completed",
                escape(message),
                dbus.Array(actions, signature="s"),
                dbus.Dictionary({
                    "urgency": dbus.Byte(1),
                    "transient": dbus.Boolean(True),
                }, signature="sv"),
                dbus.Int32(_ANCS_EXPIRE_MS),
            )
        except dbus.exceptions.DBusException as e:
            log.debug("libnotify action feedback failed: %s", e.get_dbus_name())
            return
        if retryable:
            retries = getattr(self, "_ancs_retries", None)
            if retries is None:
                retries = self._ancs_retries = {}
            retries[int(nid)] = (uid, positive, token)
            while len(retries) > MAX_ANCS_ACTION_POPUPS:
                retries.pop(next(iter(retries)))

    # ---- optional phone calls ---------------------------------------------

    def _call_maps(self) -> tuple[dict[int, str], dict[str, int]]:
        if not hasattr(self, "_call_notifications"):
            self._call_notifications = {}
            self._call_popups = {}
        return self._call_notifications, self._call_popups

    def handle_call(self, event: CallEvent) -> None:
        """Show a ringing call with Answer/Decline; close it once it stops."""
        record = event.call
        notifications, popups = self._call_maps()
        if event.kind == "call_ended" or not record.ringing:
            nid = popups.pop(record.call_id, None)
            if nid is None:
                return
            notifications.pop(nid, None)
            try:
                self._notif.CloseNotification(dbus.UInt32(nid))
            except dbus.exceptions.DBusException as error:
                log.debug("could not close call popup: %s", error.get_dbus_name())
            return
        if record.call_id in popups or self._policy() == NO_NOTIFICATIONS:
            return
        heading = "Call waiting" if record.state == "waiting" else "Incoming call"
        title = escape(terminal_text(f"\U0001f4de {heading}").replace("\n", " "))
        if config.SHOW_NOTIFICATION_CONTENT:
            peer = record.display_peer
            if record.number and peer != record.number:
                peer = f"{peer}\n{record.number}"
            body = escape(terminal_text(peer))
        else:
            # Like message popups, hidden content keeps the caller off screen.
            body = heading
        # contacts_only deliberately does not apply: a call from an unknown
        # number still needs a chance to be answered or declined.
        actions: list[str] = []
        if getattr(self, "_on_call_action", None) is not None:
            actions = ["answer", "Answer", "decline", "Decline"]
        try:
            nid = int(self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "call-start",
                title,
                body,
                dbus.Array(actions, signature="s"),
                dbus.Dictionary({
                    "urgency": dbus.Byte(2),
                    # The call itself is the record; do not keep stale
                    # "incoming call" entries in the notification history.
                    "transient": dbus.Boolean(True),
                }, signature="sv"),
                dbus.Int32(_CALL_EXPIRE_MS),
            ))
        except dbus.exceptions.DBusException as error:
            log.error("libnotify Notify (call) failed: %s", error.get_dbus_name())
            return
        notifications[nid] = record.call_id
        popups[record.call_id] = nid

    def handle_phone_battery_low(self, percent: int, *, exact: bool = False) -> None:
        """One desktop warning per discharge cycle (the daemon decides when)."""
        if self._policy() == NO_NOTIFICATIONS:
            return
        level = max(0, min(100, int(percent)))
        body = (
            f"{level} % left." if exact
            else f"About {level} % left (the phone reports 20 % steps)."
        )
        try:
            self._notif.Notify(
                _APP_NAME,
                dbus.UInt32(0),
                "battery-caution",
                "\U0001f50b iPhone battery low",
                body,
                dbus.Array([], signature="s"),
                dbus.Dictionary({"urgency": dbus.Byte(1)}, signature="sv"),
                dbus.Int32(config.NOTIFICATION_TIMEOUT_MS),
            )
        except dbus.exceptions.DBusException as error:
            log.error("libnotify Notify (battery) failed: %s", error.get_dbus_name())

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

    def _on_activation_token(self, nid, token) -> None:
        """The notification server supplies a single-use token before the action."""
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            return
        tracked = nid_i in self._open_messages or nid_i in getattr(self, "_open_apps", {})
        if tracked and len(str(token)) <= 4096:
            self._activation_tokens[nid_i] = str(token)

    def _on_action(self, nid, action) -> None:
        """Route a notification click to one client, starting it if necessary."""
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            return
        if str(action) in (
            _ANCS_POSITIVE_ACTION, _ANCS_NEGATIVE_ACTION, _ANCS_RETRY_ACTION,
        ):
            self._invoke_ancs_action(nid_i, str(action))
            return
        call_id = getattr(self, "_call_notifications", {}).get(nid_i)
        if call_id is not None:
            callback = getattr(self, "_on_call_action", None)
            if str(action) in _CALL_ACTIONS and callback is not None:
                callback(call_id, str(action))
            return
        if str(action) != "default":
            return
        handle = getattr(self, "_open_messages", {}).get(nid_i)
        callback = getattr(self, "_on_open_message", None)
        token = getattr(self, "_activation_tokens", {}).pop(nid_i, "")
        if handle and callback is not None:
            callback(handle, token)
            return
        self._activate_app_popup(nid_i, token)

    def open_click(self, click_id: str, token: str) -> bool:
        """A shell ran a popup's argv; treat it exactly like a live click."""
        if not isinstance(click_id, str) or len(click_id) > MAX_CLICK_ID_CHARS:
            return False
        nid = getattr(self, "_click_ids", {}).get(click_id)
        if nid is not None:
            return self._activate_app_popup(nid, token)
        # The popup is gone. Only a dismissal within the grace period still
        # counts, and it goes through the current rule and the throttle too.
        dismissed = self._live_dismissed_clicks()
        entry = dismissed.get(click_id)
        if entry is None or not self._open_app_target(entry[0], token):
            return False
        del dismissed[click_id]
        return True

    def _live_dismissed_clicks(self) -> dict[str, tuple[str, float]]:
        dismissed = getattr(self, "_dismissed_clicks", None)
        if dismissed is None:
            dismissed = self._dismissed_clicks = {}
        now = time.monotonic()
        for stale in [
            key for key, (_app_id, deadline) in dismissed.items() if now >= deadline
        ]:
            del dismissed[stale]
        return dismissed

    def _keep_dismissed_click(self, nid: int, app_id: str) -> None:
        dismissed = self._live_dismissed_clicks()
        deadline = time.monotonic() + _DISMISSED_CLICK_GRACE_S
        for click_id, value in getattr(self, "_click_ids", {}).items():
            if value == nid:
                dismissed[click_id] = (app_id, deadline)
        while len(dismissed) > MAX_NOTIFICATION_CLICK_TRACKERS:
            del dismissed[next(iter(dismissed))]

    def _activate_app_popup(self, nid: int, token: str) -> bool:
        # A click rule fires once per popup. The tracker is consumed only
        # when a launch was requested, so a throttled click leaves the popup
        # clickable instead of dead.
        open_apps = getattr(self, "_open_apps", {})
        app_id = open_apps.get(nid)
        if not app_id or not self._open_app_target(app_id, token):
            return False
        open_apps.pop(nid, None)
        self._forget_click_id(nid)
        return True

    def _forget_click_id(self, nid: int) -> None:
        click_ids = getattr(self, "_click_ids", {})
        for click_id in [key for key, value in click_ids.items() if value == nid]:
            del click_ids[click_id]

    def _open_app_target(self, app_id: str, token: str) -> bool:
        """Open the rule configured for this app; never the notification text."""
        target = self._resolve_open_target(app_id)
        callback = getattr(self, "_on_open_target", None)
        if target is None or callback is None:
            return False
        # A misbehaving notification server must not turn repeated action
        # signals into a stream of launches of the same target. Different
        # targets are independent, so two mapped popups clicked in quick
        # succession both open.
        now = time.monotonic()
        recent = getattr(self, "_recent_open_targets", None)
        if recent is None:
            recent = self._recent_open_targets = {}
        for stale in [
            key for key, opened in recent.items()
            if now - opened >= _OPEN_TARGET_INTERVAL_S
        ]:
            del recent[stale]
        if target in recent:
            log.info("ignoring a repeated notification click")
            return False
        recent[target] = now
        callback(target, token)
        return True

    def _on_closed(self, nid, reason) -> None:
        try:
            nid_i = int(nid)
            reason_i = int(reason)
        except (TypeError, ValueError):
            return

        getattr(self, "_open_messages", {}).pop(nid_i, None)
        app_id = getattr(self, "_open_apps", {}).pop(nid_i, None)
        if app_id and reason_i == _REASON_DISMISSED:
            # A shell that runs the popup's argv dismisses the popup first.
            self._keep_dismissed_click(nid_i, app_id)
        self._forget_click_id(nid_i)
        getattr(self, "_activation_tokens", {}).pop(nid_i, None)
        # Closing an ANCS popup, for any reason, never runs an iPhone action.
        getattr(self, "_ancs_actions", {}).pop(nid_i, None)
        getattr(self, "_ancs_retries", {}).pop(nid_i, None)
        call_id = getattr(self, "_call_notifications", {}).pop(nid_i, None)
        if call_id is not None:
            getattr(self, "_call_popups", {}).pop(call_id, None)
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
        if not config.MARK_READ_ON_DISMISS:
            return
        try:
            self._defer_mark_read(message_path)
        except Exception as error:
            log.debug("could not defer mark-read for %s: %s", message_path, error)
