"""Shared BlueZ OBEX Transfer1 completion handling."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

import dbus

from blueferry.bus import obex

log = logging.getLogger(__name__)

_TRANSFER_IFACE = "org.bluez.obex.Transfer1"
_DISAPPEARED_ERRORS = frozenset({
    "org.freedesktop.DBus.Error.UnknownObject",
    "org.bluez.obex.Error.NotFound",
})


class TransferFailed(RuntimeError):
    """BlueZ reported an explicit transfer failure."""


def _dbus_error_name(error: dbus.exceptions.DBusException) -> str:
    try:
        return str(error.get_dbus_name() or "")
    except (AttributeError, TypeError):
        return ""


def wait_for_transfer(
    transfer_path: str,
    *,
    initial_status: str = "queued",
    timeout_s: float,
    overall_timeout_s: float | None = None,
    poll_interval_s: float = 0.1,
    property_timeout_s: float | None = None,
    get_status: Callable[[], str] | None = None,
    check_progress: Callable[[], None] | None = None,
    get_progress: Callable[[], int] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Wait for one Transfer1 object and return ``complete`` or ``gone``.

    BlueZ removes short-lived transfer objects soon after completion. Only an
    explicit UnknownObject/NotFound error represents that successful race;
    unrelated bus failures remain failures. A non-terminal status at the
    deadline is always a timeout, never an implicit success. When
    ``get_progress`` is supplied, the deadline measures inactivity and is
    restarted whenever its integer value increases. ``overall_timeout_s``
    optionally imposes a hard deadline that progress cannot extend.
    """
    try:
        status = str(initial_status or "queued").casefold()
        if check_progress is not None:
            check_progress()
        status_reader = get_status
        if status_reader is None and status not in {"complete", "error"}:
            properties = obex(transfer_path, "org.freedesktop.DBus.Properties")

            def read_status() -> str:
                kwargs = (
                    {"timeout": property_timeout_s}
                    if property_timeout_s is not None else {}
                )
                return str(properties.Get(_TRANSFER_IFACE, "Status", **kwargs))

            status_reader = read_status

        timeout = max(0.0, float(timeout_s))
        started_at = monotonic()
        deadline = started_at + timeout
        overall_deadline = (
            started_at + max(0.0, float(overall_timeout_s))
            if overall_timeout_s is not None
            else None
        )
        last_progress = get_progress() if get_progress is not None else None
        while status not in {"complete", "error"}:
            if get_progress is not None:
                progress = get_progress()
                if last_progress is None or progress > last_progress:
                    last_progress = progress
                    deadline = monotonic() + timeout
            now = monotonic()
            if overall_deadline is not None and now >= overall_deadline:
                raise TimeoutError(
                    f"OBEX transfer {transfer_path} exceeded its "
                    f"{overall_timeout_s:g}s overall limit "
                    f"(last status: {status or 'unknown'})"
                )
            if now >= deadline:
                raise TimeoutError(
                    f"OBEX transfer {transfer_path} timed out after {timeout_s:g}s "
                    f"(last status: {status or 'unknown'})"
                )
            if status_reader is None:
                raise RuntimeError("OBEX transfer has no status reader")
            try:
                status = str(status_reader()).casefold()
            except dbus.exceptions.DBusException as error:
                if _dbus_error_name(error) in _DISAPPEARED_ERRORS:
                    return "gone"
                raise RuntimeError(
                    f"could not read OBEX transfer status for {transfer_path}: "
                    f"{_dbus_error_name(error) or error}"
                ) from error
            if check_progress is not None:
                check_progress()
            if status not in {"complete", "error"}:
                sleep(poll_interval_s)
        if status == "error":
            raise TransferFailed(f"OBEX transfer reported error: {transfer_path}")
        return "complete"
    except Exception:
        # Unlinking a download does not stop obexd writing through its open fd.
        try:
            obex(transfer_path, _TRANSFER_IFACE).Cancel(timeout=2.0)
        except Exception:
            log.debug("Could not cancel OBEX transfer %s", transfer_path, exc_info=True)
        raise
