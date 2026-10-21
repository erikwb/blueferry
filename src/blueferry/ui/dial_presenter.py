"""Pure presentation rules for confirming a clicked phone link in GTK.

The wording matches the Qt client's DialLinkDialog.qml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from blueferry.i18n import _
from blueferry.models import BackendStatus

# What the dialog offers besides closing it.
DialOffer = Literal["call", "settings", "none"]


@dataclass(frozen=True, slots=True)
class DialPrompt:
    heading: str
    body: str
    offer: DialOffer


def dial_prompt(
    number: str, problem: str, contact_name: str, status: BackendStatus | None,
) -> DialPrompt:
    """Describe the dialog for a parsed link (see ``tel_links.dial_request``).

    ``status`` is None when the background service could not be asked.
    """
    if problem == "invalid":
        return DialPrompt(
            _("Can't Call This Number"),
            _(
                "This link does not contain a phone number BlueFerry can call. "
                "Only plain numbers (an optional + and digits) are dialed."
            ),
            "none",
        )
    if problem == "emergency":
        return DialPrompt(
            _("Can't Call This Number"),
            _(
                "Emergency numbers are not dialed through BlueFerry. "
                "Call {number} on the iPhone itself."
            ).format(number=number),
            "none",
        )
    unavailable = _("Phone Calls Unavailable")
    if status is None:
        return DialPrompt(
            unavailable,
            _(
                "BlueFerry's background service is not reachable, so {number} "
                "cannot be called now."
            ).format(number=number),
            "settings",
        )
    if status.calls_enabled is not True:
        return DialPrompt(
            unavailable,
            _(
                "Calling {number} needs phone calls through this computer, which "
                "are turned off. Turn them on under Phone Calls in the iPhone settings."
            ).format(number=number),
            "settings",
        )
    if status.calls_state in ("connecting", "searching"):
        return DialPrompt(
            unavailable,
            _(
                "Phone calls are still connecting to the iPhone. "
                "You can call {number} as soon as they are ready."
            ).format(number=number),
            "settings",
        )
    if status.calls_state != "ready":
        return DialPrompt(
            unavailable,
            _(
                "Phone calls are on, but the iPhone's hands-free connection is "
                "not available ({state}). See \"Phone calls\" in the BlueFerry "
                "documentation."
            ).format(state=status.calls_state),
            "settings",
        )
    if contact_name:
        body = _("Call {name} from your iPhone?").format(name=contact_name)
        return DialPrompt(_("Place Call?"), f"{body}\n{number}", "call")
    return DialPrompt(
        _("Place Call?"), _("Call {name} from your iPhone?").format(name=number), "call",
    )
