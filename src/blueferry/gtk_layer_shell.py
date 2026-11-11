"""Pin small GTK 4 windows above other windows with gtk4-layer-shell.

gtk4-layer-shell only works when its library is loaded before GTK connects to
Wayland, so :func:`preload` runs before anything imports ``Gtk``. A library
outside the linker's search path (for example one built into ``~/.local``) is
named with ``BLUEFERRY_GTK4_LAYER_SHELL_LIB``; its typelib then needs
``GI_TYPELIB_PATH``. Without the library, or on a compositor without the layer
shell protocol (GNOME), :func:`place_top_center` returns ``False`` and the
caller shows an ordinary window instead.
"""
from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Mapping
from typing import Any

ENV_LIBRARY = "BLUEFERRY_GTK4_LAYER_SHELL_LIB"
LIBRARY_NAME = "libgtk4-layer-shell.so.0"
NAMESPACE = "blueferry-quick-reply"


def preload(
    environ: Mapping[str, str] = os.environ,
    loader: Callable[..., object] = ctypes.CDLL,
) -> str | None:
    """Load gtk4-layer-shell for a Wayland session; the loaded path, or ``None``."""
    backend = environ.get("GDK_BACKEND", "").split(",")[0].strip()
    if backend and backend != "wayland":
        return None
    if not environ.get("WAYLAND_DISPLAY"):
        return None
    path = environ.get(ENV_LIBRARY) or LIBRARY_NAME
    try:
        loader(path, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        return None
    return path


def place_top_center(window: Any, margin_top: int) -> bool:
    """Make ``window`` an overlay anchored to the top centre of its screen.

    Returns ``False``, leaving the window untouched, when gtk4-layer-shell or
    the compositor's support for it is missing.
    """
    try:
        import gi

        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as LayerShell
    except (ImportError, ValueError):
        return False
    if not LayerShell.is_supported():
        return False
    LayerShell.init_for_window(window)
    LayerShell.set_namespace(window, NAMESPACE)
    LayerShell.set_layer(window, LayerShell.Layer.OVERLAY)
    LayerShell.set_anchor(window, LayerShell.Edge.TOP, True)
    LayerShell.set_margin(window, LayerShell.Edge.TOP, margin_top)
    LayerShell.set_keyboard_mode(window, LayerShell.KeyboardMode.ON_DEMAND)
    return True
