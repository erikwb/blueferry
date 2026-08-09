"""Human-readable local timestamps shared by every client."""

from __future__ import annotations

from datetime import datetime


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_reference_timezone(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    if reference.tzinfo is not None:
        return value.astimezone(reference.tzinfo).replace(tzinfo=None)
    return value.astimezone().replace(tzinfo=None)


def _clock_time(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{value:%M} {value:%p}"


def format_message_timestamp(
    value: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Format an ISO timestamp relative to the local calendar.

    The result deliberately avoids minute-by-minute labels such as "5 minutes
    ago", which become stale while a conversation remains open.
    """
    if not value:
        return ""
    raw = str(value).strip()
    parsed = _parse(raw)
    if parsed is None:
        return raw.replace("\r", " ").replace("\n", " ")[:32]

    reference = now or datetime.now().astimezone()
    local = _in_reference_timezone(parsed, reference)
    if reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)

    days_ago = (reference.date() - local.date()).days
    if days_ago == 0:
        day = "Today"
    elif days_ago == 1:
        day = "Yesterday"
    elif 1 < days_ago < 7:
        day = local.strftime("%A")
    elif local.year == reference.year:
        day = f"{local:%b} {local.day}"
    else:
        day = f"{local:%b} {local.day}, {local.year}"
    return f"{day} at {_clock_time(local)}"
