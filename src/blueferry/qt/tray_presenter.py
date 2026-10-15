"""Pure presentation logic for the BlueFerry system tray item.

Kept free of Qt so the badge and tooltip are unit-testable. Everything here
works on the already decoded GetStatus and ListThreads replies; nothing
performs I/O.
"""
from __future__ import annotations

from collections.abc import Iterable

from blueferry.i18n import _, ngettext
from blueferry.models import BackendStatus, Thread, phone_status_fields

MAX_BADGE = 99


def unread_total(threads: Iterable[Thread]) -> int:
    return sum(max(0, thread.unread_count) for thread in threads)


def badge_text(count: int) -> str:
    """The number painted over the phone icon; empty when nothing is unread."""
    if count <= 0:
        return ""
    return f"{MAX_BADGE}+" if count > MAX_BADGE else str(count)


def connection_line(status: BackendStatus | None, error: str = "") -> str:
    if status is None:
        return error or _("BlueFerry service is not running")
    if status.map:
        return _("iPhone connected")
    if status.initializing:
        return _("Connecting…")
    return _("iPhone offline")


def tooltip(status: BackendStatus | None, unread: int, error: str = "") -> str:
    """Plain-text tooltip: connection, battery/signal/network, unread count."""
    lines = [connection_line(status, error)]
    if status is not None:
        lines.extend(
            _("{label}: {value}").format(label=label, value=value)
            for label, value in phone_status_fields(status)
        )
    if unread > 0:
        lines.append(
            ngettext("{count} unread message", "{count} unread messages", unread)
            .format(count=unread)
        )
    return "\n".join(lines)
