"""Toolkit-neutral texts for the manual iPhone reconnect (all clients).

Also the shared wording for a daemon rate limit (``*.RateLimited``), so no
client shows the bare "too many requests".
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _

UNREACHABLE_TEXT = _("iPhone not reachable. Is Bluetooth switched on on the iPhone?")
STARTED_TEXT = _("Reconnecting to the iPhone…")
IN_PROGRESS_TEXT = _("A reconnect is already running.")
CONNECTED_TEXT = _("The iPhone is connected.")
BLUEZ_HUNG_TEXT = _(
    "The system Bluetooth service is not responding (stuck in the kernel). "
    "Restart the computer."
)
BLUEZ_UNRESPONSIVE_TEXT = _(
    "The system Bluetooth service is not responding. If this persists, restart the computer."
)
UNSUPPORTED_TEXT = _("The running BlueFerry service cannot reconnect on request.")
PROFILE_RESET_TEXT = _("Resetting the iPhone's Bluetooth connection…")
CALL_ACTIVE_TEXT = _(
    "A call is active. The stuck connection is reset after the call; "
    "try Reconnect again then."
)
RATE_LIMITED_TEXT = _("Please wait a moment, then try again.")
UNSUPPORTED_AUTO_RESET_TEXT = _(
    "The running BlueFerry service has no automatic connection reset."
)
# profile_reset reason tokens (GetStatus profile_reset_suggested/_last).
_PROFILE_REASONS = {
    "hfp_powered_timeout": _("Call audio (HFP) is stuck."),
    "manual": _("Reset on request."),
}


def auto_reset_text(enabled: bool) -> str:
    """Confirmation after saving the automatic profile reset opt-out."""
    if enabled:
        return _("A stuck iPhone connection is now reset automatically.")
    return _("A stuck iPhone connection is no longer reset automatically; use Reconnect.")


def is_rate_limited(error_name: str) -> bool:
    return str(error_name or "").endswith(".RateLimited")


def profile_reason_text(reason: object) -> str:
    """Text for a content-free profile_reset reason token ("" when unknown)."""
    return _PROFILE_REASONS.get(reason, "") if isinstance(reason, str) else ""


@dataclass(frozen=True, slots=True)
class ReconnectView:
    """Whether to offer "Reconnect", and the line shown next to it.

    ``attention`` marks states worth a banner (the phone is unreachable or
    parked); plain "reconnecting automatically" is not.
    """

    available: bool
    offered: bool
    hint: str
    attention: bool = False


def _duration(seconds: int) -> str:
    if seconds < 60:
        return _("{seconds} s").format(seconds=seconds)
    return _("{minutes} min").format(minutes=(seconds + 59) // 60)


def reconnect_view(status: Mapping[str, Any] | None) -> ReconnectView:
    """Describe the Classic reconnect state from content-free status keys."""
    state = None if status is None else status.get("phone_reconnect_state")
    if not isinstance(state, str) or status is None or status.get("daemon") is False:
        return ReconnectView(False, False, UNSUPPORTED_TEXT)
    if status.get("bluez_unresponsive") is True:
        return ReconnectView(True, True, bluez_text(status), attention=True)
    if state == "connected":
        stuck = profile_reason_text(status.get("profile_reset_suggested"))
        if stuck:
            # Connected, but a profile hangs: Reconnect resets the device.
            return ReconnectView(True, True, stuck + " " + _(
                "Reconnect resets the iPhone's Bluetooth connection."
            ), attention=True)
        return ReconnectView(True, False, CONNECTED_TEXT)
    if state == "connecting":
        return ReconnectView(True, False, STARTED_TEXT)
    raw_next = status.get("phone_reconnect_next_in_sec")
    next_in = raw_next if type(raw_next) is int and raw_next > 0 else 0
    if status.get("phone_reconnect_paused") is True:
        hint = _("Waiting for the iPhone; next automatic try in {time}.").format(
            time=_duration(next_in),
        )
        return ReconnectView(True, True, hint, attention=True)
    if state == "unreachable" and next_in:
        hint = UNREACHABLE_TEXT + " " + _("Next automatic try in {time}.").format(
            time=_duration(next_in),
        )
        return ReconnectView(True, True, hint, attention=True)
    return ReconnectView(True, True, _("Reconnecting automatically."))


def bluez_text(status: Mapping[str, Any] | None) -> str:
    """Why nothing connects while bluetoothd does not answer."""
    reason = None if status is None else status.get("bluez_unresponsive_reason")
    return BLUEZ_HUNG_TEXT if reason == "kernel" else BLUEZ_UNRESPONSIVE_TEXT


def reconnect_error_text(error_name: str) -> str:
    """Text for a failed ReconnectPhone() call, by D-Bus error name."""
    if error_name.endswith((".UnknownMethod", ".UnknownInterface")):
        return UNSUPPORTED_TEXT
    if is_rate_limited(error_name):
        return RATE_LIMITED_TEXT
    if error_name.endswith(".NotReady"):
        return _("BlueFerry is not supervising an iPhone yet.")
    return _("The reconnect request failed.")


def result_text(result: str) -> str:
    """User-facing text for a ReconnectPhone() reply."""
    return {
        "started": STARTED_TEXT,
        "profile-reset": PROFILE_RESET_TEXT,
        "call-active": CALL_ACTIVE_TEXT,
        "in-progress": IN_PROGRESS_TEXT,
        "connected": CONNECTED_TEXT,
        "unreachable": UNREACHABLE_TEXT,
        "bluez-kernel": BLUEZ_HUNG_TEXT,
        "bluez-unresponsive": BLUEZ_UNRESPONSIVE_TEXT,
    }.get(result, UNREACHABLE_TEXT)


_NO_STATE = object()


class ManualReconnect:
    """The line a client shows after the user pressed Reconnect.

    Shared by the Qt and GTK clients: the ``ReconnectPhone()`` answer sets a
    notice that replaces the status hint until the status shows the outcome
    (connected, unreachable or waiting again). Both methods return an error
    text for the client's error display, or ``""``.
    """

    def __init__(self) -> None:
        self.pending = False
        self.notice = ""
        # A reply that ends the attempt at once (a call is active, one is
        # already running, BlueZ hangs) keeps its notice until the reconnect
        # state moves on from the one the next status shows.
        self._held_state: object = _NO_STATE

    def answered(self, result: object) -> str:
        result = str(result)
        self._held_state = _NO_STATE
        if result == "unreachable":
            self.pending = False
            self.notice = ""
            return UNREACHABLE_TEXT
        self.pending = result in ("started", "profile-reset")
        self.notice = result_text(result)
        return ""

    def follow(self, status: Mapping[str, Any] | None) -> str:
        """Turn the status outcome of a manual attempt into a clear message."""
        if not self.pending:
            if self.notice:
                state = None if status is None else status.get("phone_reconnect_state")
                if self._held_state is _NO_STATE:
                    self._held_state = state
                elif state != self._held_state:
                    self.notice = ""
            return ""
        state = None if status is None else status.get("phone_reconnect_state")
        if state in ("connected", "unreachable", "waiting"):
            self.pending = False
            self.notice = ""
        return UNREACHABLE_TEXT if state == "unreachable" else ""

    def hint(self, view: ReconnectView) -> str:
        return self.notice or view.hint
