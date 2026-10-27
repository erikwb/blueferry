"""Toolkit-neutral access to a ``photos`` plugin for every client.

Functions marked *blocking* call the plugin over D-Bus (with timeouts) and
belong on a worker thread or in the CLI. Anything coming from the plugin is
already validated by :class:`~blueferry.plugin_api.client.PluginClient`;
labels built here are plain text.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from blueferry import __version__
from blueferry.contact_photos import image_dimensions
from blueferry.i18n import _
from blueferry.plugin_api import CAPABILITY_PHOTOS
from blueferry.plugin_api.client import Photo, PluginClient, PluginError
from blueferry.plugin_api.manifest import Discovery, PluginManifest, discover

SETUP_COMMAND = "blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich"
DEFAULT_LIMIT = 60
_TYPE_TEXT = {"image": _("Photo"), "video": _("Video"), "other": _("File")}
# A thumbnail is small. A larger file, or a header declaring a larger canvas,
# stays a placeholder: a hostile cache file must not make a client decode
# gigabytes of pixels (the GTK client applies the same limits).
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
MAX_THUMBNAIL_PIXELS = 40_000_000
# Enough for the frame header behind EXIF/XMP/ICC segments of real JPEGs.
_THUMBNAIL_HEAD_BYTES = 512 * 1024


def find_plugin(
    found: Discovery | None = None, disabled: frozenset[str] | None = None,
) -> PluginManifest | None:
    """The first enabled plugin with the ``photos`` capability (no I/O but files)."""
    found = found if found is not None else discover(blueferry_version=__version__)
    if disabled is None:
        from blueferry.plugin_manager import PluginManager

        disabled = PluginManager().disabled()
    plugins = [p for p in found.with_capability(CAPABILITY_PHOTOS) if p.id not in disabled]
    return plugins[0] if plugins else None


def not_installed_hint() -> str:
    return _(
        "No photo plugin is installed. Add Immich photos in Settings > Plugins, or run: {command}"
    ).format(
        command=SETUP_COMMAND,
    )


@dataclass(frozen=True, slots=True)
class PhotosSnapshot:
    present: bool
    ready: bool
    hint: str
    photos: list[Photo] = field(default_factory=list)


def load_recent(
    manifest: PluginManifest | None,
    limit: int = DEFAULT_LIMIT,
    *,
    client_factory: Callable[[PluginManifest], PluginClient] = PluginClient,
) -> PhotosSnapshot:
    """*Blocking.* Status plus the newest photos; never raises PluginError."""
    if manifest is None:
        return PhotosSnapshot(False, False, not_installed_hint())
    client = client_factory(manifest)
    try:
        status = client.status()
        if status.state == "unconfigured":
            hint = status.detail or _("Configure the plugin in Settings > Plugins.")
            return PhotosSnapshot(True, False, hint)
        photos = client.list_recent(limit)
    except PluginError as error:
        return PhotosSnapshot(True, False, _("Photos unavailable: {reason}").format(reason=error))
    hint = _("{count} recent items from {server}").format(
        count=len(photos), server=status.server or manifest.name,
    )
    return PhotosSnapshot(True, True, hint, photos)


def thumbnail_dimensions(head: bytes) -> tuple[int, int] | None:
    """Header-declared ``(width, height)`` of a PNG, JPEG or WebP, without
    decoding; ``None`` for anything else or a truncated header."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _webp_dimensions(head)
    return image_dimensions(head)


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    # RIFF header (12), then the first chunk: FourCC (4), size (4), payload.
    kind, payload = data[12:16], data[20:]
    if kind == b"VP8 ":
        # Frame tag (3), start code 9d 01 2a, 14-bit width and height.
        if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return width, height
    if kind == b"VP8L":
        # Signature 0x2f, then 14-bit width-1 and height-1 (little-endian bits).
        if len(payload) < 5 or payload[0] != 0x2F:
            return None
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        # Flags (1), reserved (3), 24-bit canvas width-1 and height-1.
        if len(payload) < 10:
            return None
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height
    return None


def safe_thumbnail(path: Path | None) -> Path | None:
    """*Blocking (file I/O).* ``path`` if it is a regular file of at most
    :data:`MAX_THUMBNAIL_BYTES` whose PNG, JPEG or WebP header declares at
    most :data:`MAX_THUMBNAIL_PIXELS`; otherwise ``None`` (a placeholder).
    Reads only the head of the file and never decodes pixels."""
    if path is None:
        return None
    try:
        # Non-blocking, so a FIFO swapped in cannot hang the worker.
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_THUMBNAIL_BYTES:
            return None
        head = os.read(fd, min(info.st_size, _THUMBNAIL_HEAD_BYTES))
    except OSError:
        return None
    finally:
        os.close(fd)
    dimensions = thumbnail_dimensions(head)
    if dimensions is None:
        return None
    width, height = dimensions
    if width <= 0 or height <= 0 or width * height > MAX_THUMBNAIL_PIXELS:
        return None
    return path


def with_safe_thumbnails(snapshot: PhotosSnapshot) -> PhotosSnapshot:
    """*Blocking (file I/O).* The snapshot with every thumbnail that fails
    :func:`safe_thumbnail` removed, so clients show a placeholder instead of
    decoding it. Run on the worker that loaded the snapshot."""
    if not snapshot.photos:
        return snapshot
    return replace(snapshot, photos=[
        replace(photo, thumbnail=safe_thumbnail(photo.thumbnail)) for photo in snapshot.photos
    ])


def fetch_original(
    manifest: PluginManifest,
    photo_id: str,
    *,
    client_factory: Callable[[PluginManifest], PluginClient] = PluginClient,
) -> Path:
    """*Blocking.* Download (or reuse) the original; raises PluginError."""
    return client_factory(manifest).fetch_original(photo_id)


def taken_text(photo: Photo) -> str:
    """Local date and time, or an empty string for an unknown timestamp."""
    if not photo.taken_at:
        return ""
    try:
        moment = datetime.fromisoformat(photo.taken_at.replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError):
        return ""


def type_text(photo: Photo) -> str:
    return _TYPE_TEXT.get(photo.type, _TYPE_TEXT["other"])


def label(photo: Photo) -> str:
    when = taken_text(photo)
    return f"{when}  {type_text(photo)}" if when else type_text(photo)
