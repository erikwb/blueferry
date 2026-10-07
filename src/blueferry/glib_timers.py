"""Helpers for GLib main-loop timers whose source ids are kept for removal."""
from __future__ import annotations

from collections.abc import Callable

Schedule = Callable[[int, Callable[[], bool]], int]


def schedule_periodic(
    schedule: Schedule,
    seconds: int,
    callback: Callable[[], bool],
    forget: Callable[[], None],
) -> int:
    """Run ``callback`` every ``seconds`` and return the new source id.

    GLib destroys a timeout source once its callback returns a false value
    or raises. ``forget`` runs on both paths, so the owner can drop the
    stored id instead of later removing a source that no longer exists,
    which GLib reports as "Source ID ... was not found". It runs from the
    callback, after the caller has stored the id this function returns.

    ``schedule`` is ``GLib.timeout_add_seconds`` or a component's injected
    fake, looked up by the caller so test patches apply.
    """

    def tick() -> bool:
        keep = False
        try:
            keep = bool(callback())
        finally:
            if not keep:
                forget()
        return keep

    return schedule(seconds, tick)
