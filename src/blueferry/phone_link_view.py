"""Toolkit-neutral presentation of the phone overview (card and tabs).

The rules live in :mod:`blueferry.phone_overview`; this module adapts them
to what the clients show: the Qt client binds the dictionaries in QML, the
GTK client reads them in ``ui/phone_card.py``, ``ui/calls.py`` and
``ui/notifications.py``, the terminal client in ``tui_phone.py``,
``tui_calls.py`` and ``tui_notifications.py``. Every string returned here is
plain text; QML renders it with Text.PlainText, GTK sets it without markup
and the terminal client renders it as Rich ``Text``.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from blueferry import phone_overview
from blueferry.calls.model import MAX_DIAL_DIGITS, MAX_DTMF_TONES
from blueferry.i18n import _
from blueferry.notification_pin_view import pin_row_extras
from blueferry.time_display import format_message_timestamp


def feature_hints(status: Mapping[str, Any]) -> dict[str, str]:
    """Why an opt-in feature is greyed out, keyed by feature; empty when usable."""
    hints: dict[str, str] = {}
    if not status.get("media_control_enabled"):
        hints["media"] = phone_overview.in_settings(
            _("Calls & Audio"), _("Show and control what the iPhone is playing"),
        )
    if status.get("calls_enabled") is not True:
        hints["calls"] = phone_overview.in_settings(
            _("Calls & Audio"), _("Enable phone calls through this computer"),
        )
    optional = {
        "phoneStatus": phone_overview.phone_status_hint(status),
        "callHistory": phone_overview.call_history_hint(status),
        "notifications": phone_overview.notifications_hint(status),
    }
    hints.update({key: text for key, text in optional.items() if text})
    proximity = phone_overview.proximity(status)
    if not proximity["available"]:
        hints["proximity"] = proximity["hint"]
    return hints


# The dial pad: each key and the letters printed under it (ITU E.161).
DIAL_KEYS: tuple[tuple[str, str], ...] = (
    ("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"),
    ("6", "MNO"), ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
)
_NOT_DIALABLE = re.compile(r"[^0-9+*#]")
_NOT_TONE = re.compile(r"[^0-9*#]")


def phone_name(devices: Iterable[Mapping[str, Any]], mac: str) -> str:
    """The configured iPhone's Bluetooth name, else "iPhone"."""
    for device in devices:
        if mac and device.get("mac") == mac and device.get("name"):
            return str(device["name"])
    return "iPhone"


def connection_text(status: Mapping[str, Any]) -> str:
    """The phone card's connection line."""
    if status.get("daemon") is not True:
        return _("BlueFerry service is not running")
    if status.get("initializing") is True:
        return _("Connecting…")
    return _("Connected") if status.get("map") is True else _("Not connected")


def dial_input(value: object) -> str:
    """What the dial pad hands to the confirmation dialog: the text as typed,
    pasted or picked, without the spaces around it.

    Nothing is filtered out. Dropping letters, an extension or ``*``/``#``
    could turn the input into a different number, so the pad keeps the text
    and ``tel_links.dial_request`` decides; the dialog explains a refusal
    (the same rule in the Qt and GTK clients).
    """
    return str(value or "").strip()


def dialable(value: object) -> str:
    """What the dial field keeps of typed or pasted text.

    Hands-free dialing takes digits, ``+``, ``*`` and ``#`` only; the
    daemon refuses ``*`` and ``#`` codes and emergency numbers itself.
    """
    return _NOT_DIALABLE.sub("", str(value or ""))[:MAX_DIAL_DIGITS]


def dial_tones(value: object) -> str:
    """What a keypad-tone field keeps: 0-9, ``*`` and ``#``, as many as the
    daemon takes (``Calls1.SendTones``, see ``calls.model.validate_dtmf``)."""
    return _NOT_TONE.sub("", str(value or ""))[:MAX_DTMF_TONES]


# Keys the keypad and the keyboard send as tones during an active call.
TONE_KEYS = frozenset("0123456789*#")
# The "Sent: …" line shows at most this many of the last tones.
MAX_SHOWN_TONES = 16


def active_call_id(calls: Iterable[Mapping[str, Any]]) -> str:
    """The call keypad tones go to: the connected one, never a ringing or
    held call (with one held and one active, the active one)."""
    for call in calls:
        if call.get("state") == "active" and call.get("ringing") is not True:
            return str(call.get("call_id") or "")
    return ""


def tones_feedback(sent: str) -> str:
    """The line under the active call after keypad tones went out."""
    return _("Sent: {tones}").format(tones=" ".join(sent)) if sent else ""


ToneSend = Callable[[str, str, Callable[[object], None], Callable[[str], None]], None]


class KeypadTones:
    """Send keypad presses to the active call, in order, one request at a time.

    ``send(call_id, tones, done, failed)`` is the client's asynchronous
    ``SendTones``; its callbacks arrive on the client's main loop. Keys
    pressed while a request runs wait and go out together with the next one,
    so fast clicks are neither lost nor reordered. ``sent`` holds the tones
    this call has sent (for :func:`tones_feedback`); a new call starts empty.
    """

    def __init__(
        self,
        send: ToneSend,
        *,
        on_changed: Callable[[], None] = lambda: None,
        on_error: Callable[[str], None] = lambda _message: None,
    ) -> None:
        self._send = send
        self._on_changed = on_changed
        self._on_error = on_error
        self._call = ""
        self._pending = ""
        self._busy = False
        self.sent = ""

    @property
    def busy(self) -> bool:
        return self._busy

    def follow(self, call_id: str) -> None:
        """The active call is now ``call_id`` ("" for none)."""
        if call_id != self._call:
            self._call, self._pending, self.sent = call_id, "", ""
            self._on_changed()

    def press(self, call_id: str, keys: str) -> bool:
        """Send ``keys`` (cleaned like :func:`dial_tones`) to ``call_id``."""
        tones = dial_tones(keys)
        if not tones or not call_id:
            return False
        self.follow(call_id)
        self._pending = (self._pending + tones)[:MAX_DTMF_TONES]
        self._flush()
        return True

    def _flush(self) -> None:
        if self._busy or not self._pending:
            return
        call, tones, self._pending = self._call, self._pending, ""
        self._busy = True

        def done(_value: object = None) -> None:
            self._busy = False
            if call == self._call:
                self.sent = (self.sent + tones)[-MAX_SHOWN_TONES:]
                self._on_changed()
            self._flush()

        def failed(message: str) -> None:
            self._busy = False
            if call == self._call:
                self._pending = ""  # the call is likely gone; do not retry blindly
            self._on_error(message)
            self._flush()

        self._send(call, tones, done, failed)


def dial_suggestions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Contact matches the dial pad offers: phone numbers, shown as stored.

    ``rows`` are ``ConversationState.contact_result_rows()``; email
    addresses cannot be dialed and are left out.
    """
    return [
        {"name": str(row.get("name") or ""), "address": str(row.get("address") or "")}
        for row in rows
        if row.get("address") and "@" not in str(row.get("address"))
    ]


def calls_hint(status: Mapping[str, Any], calls_state: str) -> str:
    """Why the dial pad cannot call yet; empty when calls are ready.

    ``calls_state`` is the latest Calls1 state (``ready``, ``connecting``,
    ``searching``, ``unavailable`` or ``disabled``).
    """
    if status.get("calls_enabled") is not True:
        return feature_hints(status).get("calls") or _("Phone calls are off.")
    if calls_state == "ready":
        return ""
    if calls_state == "connecting":
        return _("Bringing the iPhone's hands-free connection online…")
    if calls_state == "unavailable":
        return _("oFono is not running, so calls are unavailable.")
    return _("Waiting for the iPhone's hands-free connection.")


def format_duration(seconds: float) -> str:
    """A call's duration: ``m:ss``, from one hour on ``h:mm:ss``."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def call_duration(started: object, now: float) -> str:
    """The duration since ``started`` (ISO 8601); ``now`` is a Unix time.

    ``""`` when ``started`` is missing or not a time.
    """
    try:
        moment = datetime.fromisoformat(str(started)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return format_duration(now - moment)


def call_started(call: Mapping[str, Any]) -> object:
    """When the running call's duration starts.

    ``active_since`` is when the call was answered (or the other side picked
    up); daemons without it only send ``first_seen``, when the call appeared.
    """
    return call.get("active_since") or call.get("first_seen")


def call_card(call: Mapping[str, Any], now: float) -> dict[str, Any]:
    """A running call (``CallInfo.to_dict()``) as the Calls tab shows it.

    ``subtitle`` is the call's state or, while it is active, its duration
    (``now`` is a Unix time; the clients tick it once a second, see
    :func:`call_started`). Answer is offered only while it rings; the
    other button holds or resumes; the last one hangs up or declines.
    Keypad tones (``tones``, ``send_tones``) are offered only while the
    call is ``active``.
    """
    ringing = call.get("ringing") is True
    state = str(call.get("state") or "")
    held = state == "held"
    if ringing:
        subtitle = _("Incoming call…")
    elif held:
        subtitle = _("On hold")
    elif state in ("dialing", "alerting"):
        subtitle = _("Calling…")
    else:
        subtitle = call_duration(call_started(call), now)
    return {
        "title": str(call.get("display_peer") or ""),
        "subtitle": subtitle,
        "ringing": ringing,
        "held": held,
        "answer": _("Answer"),
        "hold": _("Resume") if held else _("Hold"),
        "hangup": _("Decline") if ringing else _("Hang Up"),
        "active": state == "active" and not ringing,
        "tones": _("Keypad tones (0-9 * #)"),
        "send_tones": _("Send Tones"),
    }


def notifications_view(info: Mapping[str, Any], opt_in_hint: str) -> dict[str, str]:
    """The Notifications tab's placeholder and its content notice.

    ``info`` is :attr:`blueferry.notification_history.NotificationHistory.info`
    and ``opt_in_hint`` the ``notifications`` entry of :func:`feature_hints`.
    ``contentHint`` is empty unless only app names and times are kept.
    """
    if opt_in_hint:
        title, explanation = _("Notification List Is Off"), opt_in_hint
    elif info.get("error"):
        title, explanation = _("Notifications Are Unavailable"), str(info["error"])
    else:
        title = _("No Notifications Yet")
        explanation = _(
            "iPhone app notifications appear here while BlueFerry is running. "
            "The list is not saved."
        )
    content_hint = ""
    if info.get("enabled") is True and info.get("content") is not True:
        content_hint = _(
            "Only app names and times are shown. Turn on \u201cShow message content "
            "in popups\u201d in Settings \u203a Notifications to see titles and text."
        )
    return {"title": title, "explanation": explanation, "contentHint": content_hint}


def notification_rows(
    records: object, muted: frozenset[str] = frozenset(), *, content: bool = True,
) -> list[dict[str, Any]]:
    """Normalize ListNotifications records for the Notifications tab.

    Each row carries the shared pin fields
    (:func:`blueferry.notification_pin_view.pin_row_extras`) and ``muted``
    for rows of apps whose popups are muted (``muted`` holds their IDs).
    Without ``content`` the title, subtitle and body stay empty.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return rows
    for record in records:
        if not isinstance(record, Mapping):
            continue
        app_id = record.get("app_id")
        app_id = app_id if isinstance(app_id, str) else ""
        app = str(record.get("app_name") or app_id or _("iPhone app"))
        title = str(record.get("title") or "") if content else ""
        subtitle = str(record.get("subtitle") or "") if content else ""
        rows.append({
            **pin_row_extras(record),
            "app": app,
            "app_id": app_id,
            "muted": bool(app_id) and app_id in muted,
            "time": format_message_timestamp(str(record.get("time") or "")),
            "title": title,
            "subtitle": subtitle,
            # The line under the app name: title and subtitle, as present.
            "summary": " · ".join(part for part in (title, subtitle) if part),
            "body": str(record.get("body") or "") if content else "",
        })
    return rows
