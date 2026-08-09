"""Pure presentation rules for the GTK setup/status page."""
from __future__ import annotations

from collections.abc import Mapping

from blueferry.i18n import _
from blueferry.onboarding import OnboardingStage


def connection_subtitle(status: Mapping, *, reachable: bool) -> str:
    if not reachable:
        return _("Not Reachable — Retrying Automatically")
    state = str(status.get("connectivity_state", "ready"))
    labels = {
        "initializing": _("Initializing"),
        "connecting": _("Connecting"),
        "ready": _("Ready"),
        "degraded": _("Limited Connectivity"),
        "reconnecting": _("Reconnecting"),
        "authorization-required": _("Authorization Required"),
        "stopping": _("Stopping"),
    }
    subtitle = labels.get(state, state.replace("-", " ").title())
    detail = str(status.get("connectivity_detail", ""))
    if state != "ready" and detail:
        subtitle = _("{state} — {detail}").format(state=subtitle, detail=detail)
    retry = int(status.get("retry_delay_seconds", 0) or 0)
    if retry:
        subtitle = _("{state}; retrying in {seconds}s").format(
            state=subtitle, seconds=retry,
        )
    return subtitle


def onboarding_presentation(
    stage: OnboardingStage, *, incompatibility: str = "",
) -> tuple[str, str, str]:
    copy = {
        OnboardingStage.CHECKING: (
            _("Checking Bluetooth Support"), _("No changes are being made")
        ),
        OnboardingStage.INCOMPATIBLE: (
            _("Bluetooth Controller Is Not Compatible"),
            incompatibility or _("No adapter found"),
        ),
        OnboardingStage.ACTIVATE_BLUETOOTH: (
            _("Activate Bluetooth Support"),
            _("Authorize one Bluetooth restart before pairing"),
        ),
        OnboardingStage.SELECT_DEVICE: (
            _("Select and Pair an iPhone"),
            _("Keep Bluetooth settings open on the unlocked iPhone"),
        ),
        OnboardingStage.STARTING: (
            _("Starting the Background Service"),
            _("This normally takes a few seconds"),
        ),
        OnboardingStage.IPHONE_SETTINGS: (
            _("Finish Setup on the iPhone"),
            _(
                "Enable Show Message Notifications and Sync Contacts; "
                "verification updates automatically"
            ),
        ),
        OnboardingStage.READY: (
            _("BlueFerry Is Ready"),
            _("Messages, contacts, and iPhone notifications are connected"),
        ),
        OnboardingStage.READY_WITHOUT_ANCS: (
            _("Messages Are Ready"),
            _("Messages and contacts work; per-app notifications are unavailable"),
        ),
    }
    title, subtitle = copy[stage]
    if stage in {OnboardingStage.READY, OnboardingStage.READY_WITHOUT_ANCS}:
        icon = "emblem-ok-symbolic"
    elif stage is OnboardingStage.INCOMPATIBLE:
        icon = "dialog-warning-symbolic"
    else:
        icon = "emblem-system-symbolic"
    return title, subtitle, icon
