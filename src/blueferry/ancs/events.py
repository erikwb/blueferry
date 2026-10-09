"""AncsEvent — normalized per-app iPhone notification.

Distinct from SmsEvent (which represents MAP-delivered SMS/iMessage) so
sinks can render them differently. Both flow through the same sink fan-out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from blueferry.ancs.constants import MESSAGES_APP_ID


@dataclass(slots=True)
class AncsEvent:
    """Minimum live notification content used by current sinks."""

    notification_id: int          # ANCS uid (4-byte, device-local)
    app_id: str                   # bundle id, e.g. "com.apple.MobileSMS"
    app_name: str                 # display name, may be empty until resolved
    title: str
    subtitle: str
    body: str

    # iOS-supplied action button labels. Empty unless BLUEFERRY_ANCS_ACTIONS
    # is enabled and the Notification Source flags announced the action.
    # They are presentation strings, never retained or put on D-Bus.
    positive_action_label: str = ""
    negative_action_label: str = ""
    # Opaque, content-free id of the offer above. A desktop click must hand
    # it back so a reused UID can never inherit an older popup's buttons.
    action_token: int = 0

    seen_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # ANCS CategoryID from the Notification Source header (0 = Other).
    category: int = 0

    @property
    def display_title(self) -> str:
        """Best human-readable title."""
        if self.title:
            return self.title
        return self.app_name or self.app_id or "Notification"

    @property
    def has_actions(self) -> bool:
        return bool(self.positive_action_label or self.negative_action_label)

    def correlation_dict(self) -> dict:
        """Return only the Messages fields required for group correlation."""
        if self.app_id != MESSAGES_APP_ID:
            raise ValueError("only Apple Messages ANCS events may be retained")
        return {
            "kind": "ancs_notification",
            "notification_id": self.notification_id,
            "app_id": self.app_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "body": self.body,
            "seen_at": self.seen_at.isoformat(),
        }
