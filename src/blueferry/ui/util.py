"""Small shared helpers for the UI pages."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib, Gtk

from blueferry.client_tasks import ScheduledTimer, Timer
from blueferry.time_display import format_message_timestamp


def glib_timer(callback: Callable[[], None]) -> Timer:
    """A ``client_tasks.TimerFactory`` over the default GLib main context."""
    return ScheduledTimer(GLib.timeout_add, GLib.source_remove, callback)


def launch_uri(
    parent: Gtk.Widget | None, uri: str, failed: Callable[[str], None] | None = None,
) -> None:
    """Open ``uri`` (https or file) in the user's default app, asynchronously.

    Callers pass only URIs that the shared models have already checked.
    """
    root = parent.get_root() if parent is not None else None
    window = root if isinstance(root, Gtk.Window) else None

    def finished(launcher: Gtk.UriLauncher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if failed is not None:
                failed(error.message or str(error))

    Gtk.UriLauncher.new(uri).launch(window, None, finished)


def format_ts(value: str | None) -> str:
    """Compatibility name used by the GTK conversation view."""
    return format_message_timestamp(value)


def event_ts(ev: dict) -> str:
    """Best timestamp string for an event dict (message timestamp or seen_at)."""
    return ev.get("timestamp") or ev.get("seen_at") or ""
