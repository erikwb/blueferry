"""Open a URI with the desktop's default handler (the CLIs' "open" step)."""
from __future__ import annotations


def open_uri(uri: str) -> None:
    """Hand ``uri`` to the default application through Gio; callers check it first."""
    from gi.repository import Gio

    Gio.AppInfo.launch_default_for_uri(uri, None)
