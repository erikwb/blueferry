"""Small shared helpers for the UI pages."""
from __future__ import annotations

from blueferry.time_display import format_message_timestamp


def format_ts(value: str | None) -> str:
    """Compatibility name used by the GTK conversation view."""
    return format_message_timestamp(value)


def event_ts(ev: dict) -> str:
    """Best timestamp string for an event dict (message timestamp or seen_at)."""
    return ev.get("timestamp") or ev.get("seen_at") or ""
