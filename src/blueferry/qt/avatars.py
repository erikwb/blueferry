"""Contact avatars for QML: an image provider over the controller's cache.

Photo bytes come from the phone's address book and are untrusted. The daemon
only checks size, the JPEG/PNG signature, and the header-declared canvas;
decoding happens here, in the presentation process, with Qt's image reader.
The header is inspected again before any pixels are allocated, and the decode
is scaled to a bounded avatar size, so a crafted image cannot request an
enormous buffer.
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote, unquote

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtQuick import QQuickImageProvider

from blueferry.contact_photos import valid_photo
from blueferry.limits import MAX_CONTACT_PHOTO_DIMENSION

AVATAR_PROVIDER = "blueferry-avatar"
DEFAULT_AVATAR_SIZE = 128
# Largest avatar side a QML request may ask for, and the decoded-image budget
# that follows from it (twice that side, 32-bit pixels).
MAX_AVATAR_SIZE = 512
MAX_AVATAR_DECODED_BYTES = (2 * MAX_AVATAR_SIZE) ** 2 * 4
_FORMATS = frozenset({"jpeg", "jpg", "png"})


def avatar_url(address: str, revision: int) -> str:
    """Image-provider URL; the revision query only defeats QML's cache."""
    return f"image://{AVATAR_PROVIDER}/{quote(address, safe='')}?r={int(revision)}"


def address_from_id(image_id: str) -> str:
    return unquote(image_id.split("?", 1)[0])


def decode_avatar(data: bytes, requested: QSize | None = None) -> QImage:
    """Decode a bounded JPEG/PNG into an avatar-sized image, or a null image.

    The decode target covers the requested square (so QML can crop it) but
    never exceeds twice the target on either side: an extreme aspect ratio
    such as 1x2048 falls back to fitting inside that bound instead of
    expanding to a huge strip. The result is checked against a byte budget.
    """
    selected = valid_photo(data)
    if selected is None:
        return QImage()
    buffer = QBuffer()
    buffer.setData(QByteArray(selected))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return QImage()
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    if bytes(reader.format().data()).decode("ascii", "replace").casefold() not in _FORMATS:
        return QImage()
    dimensions = reader.size()
    if (
        not dimensions.isValid()
        or dimensions.width() > MAX_CONTACT_PHOTO_DIMENSION
        or dimensions.height() > MAX_CONTACT_PHOTO_DIMENSION
    ):
        return QImage()
    width = height = DEFAULT_AVATAR_SIZE
    if requested is not None and requested.width() > 0 and requested.height() > 0:
        width, height = requested.width(), requested.height()
    target = QSize(min(width, MAX_AVATAR_SIZE), min(height, MAX_AVATAR_SIZE))
    limit = QSize(2 * target.width(), 2 * target.height())
    scaled = dimensions.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    if scaled.width() > limit.width() or scaled.height() > limit.height():
        scaled = dimensions.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio)
    reader.setAutoTransform(True)
    reader.setScaledSize(scaled.expandedTo(QSize(1, 1)))
    image = reader.read()
    if image.isNull() or image.sizeInBytes() > MAX_AVATAR_DECODED_BYTES:
        return QImage()
    return image


class AvatarImageProvider(QQuickImageProvider):
    """Serve ``image://blueferry-avatar/<address>`` from already-fetched bytes.

    The provider never performs D-Bus I/O; the controller fetches bytes on its
    worker and exposes them through ``lookup``, which must be thread-safe.
    """

    def __init__(self, lookup: Callable[[str], bytes | None]) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._lookup = lookup

    def requestImage(self, image_id: str, size: QSize, requested_size: QSize) -> QImage:
        data = self._lookup(address_from_id(image_id))
        return decode_avatar(data, requested_size) if data else QImage()
