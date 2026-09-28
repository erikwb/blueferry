"""Opt-in contact photos: vCard PHOTO decoding and volatile notification files.

Threat model
------------
A PHOTO value is attacker-controllable input from the phone's address book
(a contact card can come from anyone who sends one). The daemon therefore
never decodes pixels. It only:

* accepts inline base64 (vCard 2.1 ``ENCODING=BASE64``, vCard 3.0
  ``ENCODING=b``, and vCard 4.0 ``data:`` URIs) and never follows a URI;
* bounds the encoded and decoded size (``limits.MAX_CONTACT_PHOTO_*``);
* accepts only bytes that start with the JPEG or PNG signature and whose
  header declares a canvas within ``MAX_CONTACT_PHOTO_DIMENSION`` (read by
  walking the header bytes, never by decoding), so a small file cannot make
  a client or notification server allocate an enormous image.

Everything that interprets image structure runs in a presentation process
with a standard toolkit loader (Qt's ``QImageReader`` in the Kirigami client,
the desktop notification server for popups), so a decoder bug cannot reach
the process that holds MAP/PBAP sessions and the storage key. Stored photos
share the contact cache's database, encryption, and replacement lifecycle.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import stat
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from blueferry.limits import (
    MAX_CONTACT_PHOTO_BYTES,
    MAX_CONTACT_PHOTO_CHARS,
    MAX_CONTACT_PHOTO_DIMENSION,
    MAX_CONTACT_PHOTO_FILES,
)
from blueferry.private_files import create_runtime_private_file, runtime_private_directory

log = logging.getLogger(__name__)

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WHITESPACE = str.maketrans("", "", " \t\r\n")


def image_type(data: bytes) -> str | None:
    """Return the MIME type for an accepted image signature, else ``None``."""
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    if data.startswith(PNG_MAGIC):
        return "image/png"
    return None


# JPEG start-of-frame markers carry the frame size (SOF0-3, 5-7, 9-11, 13-15).
_JPEG_SOF = frozenset({
    *range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0),
})
# Markers without a length field: TEM and RST0-7 (SOI/EOI are handled apart).
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
# Real encoders put the frame header within a handful of segments and use no
# fill bytes. These caps keep the walk cheap on hostile input: segment lengths
# make each step jump, so without the fill cap a run of 0xFF bytes would be
# the only per-byte path.
_JPEG_MAX_SEGMENTS = 256
_JPEG_MAX_FILL_BYTES = 64


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # Signature (8), IHDR length (4), "IHDR" (4), width (4), height (4).
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk JPEG marker segments up to the first frame header, bounded by size."""
    position = 2  # after SOI
    end = len(data)
    segments = fill = 0
    while position + 1 < end:
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker == 0xFF:  # fill byte before a marker
            fill += 1
            if fill > _JPEG_MAX_FILL_BYTES:
                return None
            position += 1
            continue
        segments += 1
        if segments > _JPEG_MAX_SEGMENTS:
            return None
        position += 2
        if marker in _JPEG_STANDALONE:
            continue
        if marker in (0xD8, 0xD9, 0xDA) or position + 2 > end:
            return None  # no frame header before scan data or end of image
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > end:
            return None
        if marker in _JPEG_SOF:
            if length < 7:
                return None
            # Height 0 means "defined later by a DNL marker" (JPEG B.2.2). That
            # is legal but unused by photo encoders, and the real size would
            # sit after scan data, so valid_photo deliberately rejects it.
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += length
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Header-declared ``(width, height)`` of a JPEG or PNG, without decoding."""
    kind = image_type(data)
    if kind == "image/png":
        return _png_dimensions(data)
    if kind == "image/jpeg":
        return _jpeg_dimensions(data)
    return None


def valid_photo(data: object) -> bytes | None:
    """Return ``data`` when it is a bounded JPEG/PNG with a bounded canvas.

    Applied when parsing, when loading from storage, before writing a
    notification file, and again in clients, so no layer can widen it.
    """
    if not isinstance(data, bytes | bytearray):
        return None
    selected = bytes(data)
    if not selected or len(selected) > MAX_CONTACT_PHOTO_BYTES:
        return None
    dimensions = image_dimensions(selected)
    if dimensions is None:
        return None
    width, height = dimensions
    if not (0 < width <= MAX_CONTACT_PHOTO_DIMENSION and 0 < height <= MAX_CONTACT_PHOTO_DIMENSION):
        return None
    return selected


def _split_unquoted(text: str, separator: str, *, first_only: bool = False) -> list[str]:
    """Split on ``separator`` outside double-quoted parameter values."""
    parts: list[str] = []
    quoted = False
    start = 0
    for index, character in enumerate(text):
        if character == '"':
            quoted = not quoted
        elif character == separator and not quoted:
            parts.append(text[start:index])
            start = index + 1
            if first_only:
                break
    parts.append(text[start:])
    return parts


def _parameters(raw: str) -> tuple[str, list[str]]:
    """Split ``[group.]PHOTO;A=B;X="c;d"`` into its name and upper-cased parameters."""
    parts = _split_unquoted(raw, ";")
    return parts[0].strip(), [part.strip().upper() for part in parts[1:]]


def decode_vcard_photo(prop: str | None) -> bytes | None:
    """Decode one unfolded PHOTO property line (``params:value``) safely.

    Returns ``None`` for URIs, unknown encodings, malformed base64, oversized
    data, or anything that is not a JPEG/PNG signature.
    """
    if not prop or len(prop) > MAX_CONTACT_PHOTO_CHARS:
        return None
    # Parameter values may be quoted and contain ":" (vCard 3.0/4.0), so the
    # value starts at the first colon outside quotes.
    split = _split_unquoted(prop, ":", first_only=True)
    if len(split) != 2:
        return None
    head, value = split
    _name, params = _parameters(head)
    encoded: str | None = None
    if any(param in ("ENCODING=B", "ENCODING=BASE64", "BASE64") for param in params):
        # vCard 3.0 ENCODING=b, vCard 2.1 ENCODING=BASE64 or bare BASE64.
        encoded = value
    elif value[:5].casefold() == "data:":
        # vCard 4.0: data:image/jpeg;base64,<data>. Other data URIs are ignored.
        meta, comma, payload = value[5:].partition(",")
        if comma and meta.strip().casefold().endswith(";base64"):
            encoded = payload
    if encoded is None:
        # VALUE=URI or an unrecognized encoding: never fetch remote content.
        return None
    compact = encoded.translate(_WHITESPACE)
    if not compact or len(compact) > MAX_CONTACT_PHOTO_CHARS:
        return None
    # Some writers omit padding; restore it rather than reject the photo.
    compact += "=" * (-len(compact) % 4)
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None
    return valid_photo(data)


class PhotoFiles:
    """Owner-only volatile copies of avatars for notification ``image-path``.

    Notification servers read an image path themselves, which keeps image
    decoding out of the daemon. Files live below ``$XDG_RUNTIME_DIR/blueferry``
    (tmpfs, mode 0700/0600), carry random names that reveal nothing about the
    contact, are bounded to ``MAX_CONTACT_PHOTO_FILES``, and are removed when
    the contact cache changes or the daemon stops.
    """

    def __init__(
        self,
        *,
        photo_ref: Callable[[str | None], int | None],
        load_photo: Callable[[int], bytes | None],
        create_file: Callable[..., tuple[int, Path]] = create_runtime_private_file,
    ) -> None:
        self._photo_ref = photo_ref
        self._load_photo = load_photo
        self._create_file = create_file
        self._files: OrderedDict[int, Path] = OrderedDict()
        # References that resolved to no usable photo (dangling or invalid)
        # are not reloaded for every popup until the cache changes.
        self._unavailable: set[int] = set()
        self._remove_orphans()

    @staticmethod
    def _remove_orphans() -> None:
        """Delete copies a crashed daemon left behind (regular files we own)."""
        try:
            directory = runtime_private_directory()
            for path in directory.glob("avatar-*"):
                try:
                    current = os.lstat(path)
                    if stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid():
                        path.unlink()
                except OSError:
                    continue
        except Exception as error:
            log.debug("could not sweep volatile avatar files: %s", type(error).__name__)

    def path_for(self, address: str | None) -> str | None:
        """Return a file path holding the unique contact's photo, if any."""
        try:
            ref = self._photo_ref(address)
            if ref is None or ref in self._unavailable:
                return None
            cached = self._files.get(ref)
            if cached is not None and cached.exists():
                self._files.move_to_end(ref)
                return str(cached)
            data = valid_photo(self._load_photo(ref))
            if data is None:
                if len(self._unavailable) < 4 * MAX_CONTACT_PHOTO_FILES:
                    self._unavailable.add(ref)
                return None
            suffix = ".png" if image_type(data) == "image/png" else ".jpg"
            descriptor, path = self._create_file(prefix="avatar-", suffix=suffix)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            self._files[ref] = path
            self._evict()
            return str(path)
        except Exception as error:
            # Popups must still appear without an avatar.
            log.debug("contact photo unavailable for a notification: %s", type(error).__name__)
            return None

    def _evict(self) -> None:
        while len(self._files) > MAX_CONTACT_PHOTO_FILES:
            _ref, path = self._files.popitem(last=False)
            path.unlink(missing_ok=True)

    def clear(self) -> None:
        """Delete every volatile avatar copy."""
        files, self._files = self._files, OrderedDict()
        self._unavailable.clear()
        for path in files.values():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.debug("could not remove a volatile avatar file", exc_info=True)

    def __len__(self) -> int:
        return len(self._files)
