"""Photos tab thumbnails for QML: an image provider over safely read bytes.

A thumbnail path was checked when the list loaded, but the file may have
been swapped since. QML's ``Image`` would open a ``file:`` URL itself and
block on a FIFO; this provider reads the file through
:func:`blueferry.photos_view.read_thumbnail` (no FIFO wait, regular files
only, bounded size) and decodes those bytes with Qt's image reader. QML
calls it on its image-loading thread, never the UI thread, even for an
``Image`` that is not ``asynchronous``.
"""
from __future__ import annotations

from urllib.parse import quote, unquote

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtQml import QQmlImageProviderBase
from PySide6.QtQuick import QQuickImageProvider

from blueferry.photos_view import MAX_THUMBNAIL_PIXELS, read_thumbnail, thumbnail_fits

THUMBNAIL_PROVIDER = "blueferry-thumbnail"
THUMBNAIL_SIZE = 256
# Largest side a QML request may ask for; the decode never exceeds twice it.
MAX_THUMBNAIL_SIZE = 512


def thumbnail_url(path: object) -> str:
    """Image-provider URL of a local thumbnail ("" for none)."""
    return f"image://{THUMBNAIL_PROVIDER}/{quote(str(path), safe='')}" if path else ""


def path_from_id(image_id: str) -> str:
    return unquote(image_id)


def decode_thumbnail(data: bytes, requested: QSize | None = None) -> QImage:
    """Decode a PNG, JPEG or WebP thumbnail covering the requested size
    (:data:`THUMBNAIL_SIZE` by default), or a null image when its canvas is
    over :data:`MAX_THUMBNAIL_PIXELS` or it cannot be read."""
    if not thumbnail_fits(data):
        return QImage()
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return QImage()
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    dimensions = reader.size()
    if (
        not dimensions.isValid() or dimensions.isEmpty()
        or dimensions.width() * dimensions.height() > MAX_THUMBNAIL_PIXELS
    ):
        return QImage()
    width = height = THUMBNAIL_SIZE
    if requested is not None and requested.width() > 0 and requested.height() > 0:
        width, height = requested.width(), requested.height()
    target = QSize(min(width, MAX_THUMBNAIL_SIZE), min(height, MAX_THUMBNAIL_SIZE))
    if dimensions.width() > target.width() or dimensions.height() > target.height():
        # Cover the cell (QML crops it), but an extreme aspect ratio such as
        # 1x8000 fits inside twice the target instead of a huge strip.
        limit = QSize(2 * target.width(), 2 * target.height())
        scaled = dimensions.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        if scaled.width() > limit.width() or scaled.height() > limit.height():
            scaled = dimensions.scaled(limit, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(scaled.expandedTo(QSize(1, 1)))
    image = reader.read()
    return QImage() if image.isNull() else image


class ThumbnailImageProvider(QQuickImageProvider):
    """Serve ``image://blueferry-thumbnail/<path>`` from the file's bytes."""

    def __init__(self) -> None:
        super().__init__(
            QQuickImageProvider.ImageType.Image,
            QQmlImageProviderBase.Flag.ForceAsynchronousImageLoading,
        )

    def requestImage(self, image_id: str, size: QSize, requested_size: QSize) -> QImage:
        data = read_thumbnail(path_from_id(image_id))
        return decode_thumbnail(data, requested_size) if data else QImage()
