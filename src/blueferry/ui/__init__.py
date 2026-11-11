"""GTK4/libadwaita presentation client for the headless backend."""
from __future__ import annotations

from blueferry.gtk_layer_shell import preload

# Before anything loads GTK: gtk4-layer-shell must be in the process first.
preload()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
