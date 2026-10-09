"""Bounded, asynchronous contact avatars for the GTK presentation process."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from gi.repository import Gdk, GdkPixbuf, Gio, GLib

from blueferry.contact_photos import valid_photo

AVATAR_SIZE = 64
MAX_AVATARS = 512
MAX_PENDING_AVATARS = 8


def decode_avatar(data: bytes) -> Gdk.Texture | None:
    """Validate and scale JPEG/PNG bytes before creating a small texture."""
    selected = valid_photo(data)
    if selected is None:
        return None
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(selected))
    try:
        pixels = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
            stream, AVATAR_SIZE, AVATAR_SIZE, True, None,
        )
        if pixels is None or pixels.get_width() > AVATAR_SIZE or pixels.get_height() > AVATAR_SIZE:
            return None
        pixel_format = (
            Gdk.MemoryFormat.R8G8B8A8 if pixels.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        )
        return Gdk.MemoryTexture.new(
            pixels.get_width(), pixels.get_height(), pixel_format,
            GLib.Bytes.new(pixels.get_pixels()), pixels.get_rowstride(),
        )
    except GLib.Error:
        return None
    finally:
        stream.close(None)


class AvatarCache:
    """Keep lookups quiet, coalesced and bounded across contact generations."""

    def __init__(
        self,
        fetch: Callable[..., None],
        changed: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._changed = changed
        self._clock = clock
        self._status: tuple[bool, object] = (False, None)
        self._generation = 0
        self._values: dict[str, Any] = {}
        self._pending: dict[str, int] = {}
        self._retry: dict[str, tuple[float, int]] = {}

    def update_status(self, status: Mapping[str, Any]) -> None:
        current = (status.get("contact_photos") is True, status.get("contact_photo_revision"))
        if current != self._status:
            self._status = current
            self._generation += 1
            self._values.clear()
            self._retry.clear()
        # Normal status refreshes also let failed decoration reads retry.
        self._changed()

    def get(self, address: str) -> Gdk.Texture | None:
        if not self._status[0] or not address:
            return None
        if address in self._values:
            return self._values[address]
        deadline, failures = self._retry.get(address, (0.0, 0))
        if (
            address in self._pending
            or len(self._pending) >= MAX_PENDING_AVATARS
            or (
                address not in self._retry
                and len(self._values) + len(self._pending) + len(self._retry) >= MAX_AVATARS
            )
            or self._clock() < deadline
        ):
            return None
        self._retry.pop(address, None)
        generation = self._generation
        self._pending[address] = generation

        def completed(value: Gdk.Texture | None) -> None:
            self._pending.pop(address, None)
            if generation == self._generation:
                self._values[address] = value
            self._changed()

        def failed(_message: str) -> None:
            self._pending.pop(address, None)
            if generation == self._generation:
                attempts = failures + 1
                self._retry[address] = (
                    self._clock() + min(30 * 2 ** min(attempts - 1, 5), 600), attempts,
                )
            self._changed()

        self._fetch(address, completed, failed)
        return None
