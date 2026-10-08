"""Human-readable local timestamps shared by every client."""

from __future__ import annotations

from datetime import datetime

from blueferry.text_safety import terminal_text

# The labels around these names ("Today", "at", "AM") are English, so the names
# are spelled out here rather than taken from strftime, whose %A, %b and %p
# follow LC_TIME once a toolkit (GTK, Qt) calls setlocale(LC_ALL, "").
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


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
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {meridiem}"


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
        single_line = raw.replace("\r", " ").replace("\n", " ")
        return terminal_text(single_line)[:32]

    # A naive local reference lets each message use its own date's DST offset.
    reference = now or datetime.now()
    local = _in_reference_timezone(parsed, reference)
    if reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)

    days_ago = (reference.date() - local.date()).days
    if days_ago == 0:
        day = "Today"
    elif days_ago == 1:
        day = "Yesterday"
    elif 1 < days_ago < 7:
        day = _WEEKDAYS[local.weekday()]
    elif local.year == reference.year:
        day = f"{_MONTHS[local.month - 1]} {local.day}"
    else:
        day = f"{_MONTHS[local.month - 1]} {local.day}, {local.year}"
    return f"{day} at {_clock_time(local)}"
