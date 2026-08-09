"""Small shared helpers for the UI pages."""
from __future__ import annotations

from datetime import datetime


def format_ts(value: str | None, *, fmt: str = "%b %-d · %H:%M") -> str:
    """Format an ISO timestamp from an event dict, in local time."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(value)[:16]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    try:
        return dt.strftime(fmt)
    except ValueError:  # %-d is glibc-only; fall back if unsupported
        return dt.strftime("%b %d · %H:%M")


def event_ts(ev: dict) -> str:
    """Best timestamp string for an event dict (message timestamp or seen_at)."""
    return ev.get("timestamp") or ev.get("seen_at") or ""
