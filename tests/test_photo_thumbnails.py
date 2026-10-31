"""Thumbnail size limits of the Photos tab (photos_view.safe_thumbnail)."""
from __future__ import annotations

import os
import struct
import zlib

import pytest

from blueferry import photos_view
from blueferry.plugin_api.client import Photo

from .photo_fixtures import jpeg, png


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png_bomb(width: int, height: int) -> bytes:
    """A complete, tiny PNG that declares an enormous canvas."""
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(b"\0" * 16))
            + _chunk(b"IEND", b""))


def _riff(kind: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + kind + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def webp_lossy(width: int, height: int) -> bytes:
    return _riff(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a"
                 + struct.pack("<HH", width, height) + bytes(8))


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return _riff(b"VP8L", b"\x2f" + struct.pack("<I", bits) + bytes(8))


def webp_extended(width: int, height: int) -> bytes:
    return _riff(b"VP8X", b"\x00" + bytes(3) + (width - 1).to_bytes(3, "little")
                 + (height - 1).to_bytes(3, "little"))


@pytest.mark.parametrize(("data", "expected"), [
    (png(3, 2), (3, 2)),
    (jpeg(5, 4), (5, 4)),
    (webp_lossy(320, 240), (320, 240)),
    (webp_lossless(16384, 1), (16384, 1)),
    (webp_extended(1 << 24, 1 << 24), (1 << 24, 1 << 24)),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", None),  # truncated
    (_riff(b"VP8 ", bytes(10)), None),  # no start code
    (_riff(b"VP8L", b"\x00" + bytes(4)), None),  # bad signature
    (_riff(b"ALPH", bytes(10)), None),  # unknown first chunk
    (b"GIF89a\x01\x00\x01\x00", None),  # not an accepted format
    (b"", None),
])
def test_thumbnail_dimensions_read_only_headers(data, expected) -> None:
    assert photos_view.thumbnail_dimensions(data) == expected


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_small_image_is_shown(tmp_path) -> None:
    for name, data in [("a.png", png(4, 4)), ("b.jpg", jpeg(64, 48)),
                       ("c.webp", webp_lossy(256, 256))]:
        path = _write(tmp_path, name, data)
        assert photos_view.safe_thumbnail(path) == path


def test_a_declared_decompression_bomb_is_a_placeholder(tmp_path) -> None:
    bomb = png_bomb(100_000, 100_000)
    assert len(bomb) < 200  # tiny on disk, 10 gigapixels when decoded
    assert photos_view.safe_thumbnail(_write(tmp_path, "bomb.png", bomb)) is None
    at_limit = _write(tmp_path, "limit.png", png_bomb(8000, 5000))  # exactly 40 MP
    assert photos_view.safe_thumbnail(at_limit) == at_limit
    over = _write(tmp_path, "over.png", png_bomb(8000, 5001))
    assert photos_view.safe_thumbnail(over) is None
    huge_webp = _write(tmp_path, "huge.webp", webp_extended(1 << 24, 1 << 24))
    assert photos_view.safe_thumbnail(huge_webp) is None
    zero = _write(tmp_path, "zero.png", png_bomb(0, 10))
    assert photos_view.safe_thumbnail(zero) is None


def test_an_oversized_file_is_a_placeholder(tmp_path) -> None:
    data = png(2, 2)
    path = _write(tmp_path, "big.png", data + bytes(photos_view.MAX_THUMBNAIL_BYTES))
    assert photos_view.safe_thumbnail(path) is None
    fits = _write(tmp_path, "fits.png",
                  data + bytes(photos_view.MAX_THUMBNAIL_BYTES - len(data)))
    assert photos_view.safe_thumbnail(fits) == fits


@pytest.mark.parametrize("data", [
    b"", b"garbage that is not an image", b"\x89PNG\r\n\x1a\n", jpeg(4, 4)[:12],
])
def test_a_truncated_or_unknown_header_is_a_placeholder(tmp_path, data) -> None:
    assert photos_view.safe_thumbnail(_write(tmp_path, "t.png", data)) is None


def test_missing_files_fifos_and_symlinks_are_placeholders(tmp_path) -> None:
    assert photos_view.safe_thumbnail(None) is None
    assert photos_view.safe_thumbnail(tmp_path / "missing.png") is None
    assert photos_view.safe_thumbnail(tmp_path) is None  # a directory
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    assert photos_view.safe_thumbnail(fifo) is None  # never blocks
    link = tmp_path / "link.png"
    link.symlink_to(_write(tmp_path, "real.png", png(2, 2)))
    assert photos_view.safe_thumbnail(link) is None


def test_with_safe_thumbnails_blanks_only_the_rejected_ones(tmp_path) -> None:
    good = _write(tmp_path, "good.png", png(2, 2))
    bomb = _write(tmp_path, "bomb.png", png_bomb(100_000, 100_000))
    original = tmp_path / "IMG_1.HEIC"
    snapshot = photos_view.PhotosSnapshot(True, True, "3 recent items", [
        Photo("a", "", "image", good),
        Photo("b", "", "image", bomb, original),
        Photo("c", "", "video", None),
    ])
    checked = photos_view.with_safe_thumbnails(snapshot)
    assert [photo.thumbnail for photo in checked.photos] == [good, None, None]
    assert checked.photos[1].original == original and checked.hint == snapshot.hint
    empty = photos_view.PhotosSnapshot(False, False, "none")
    assert photos_view.with_safe_thumbnails(empty) is empty
