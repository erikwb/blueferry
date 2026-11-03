"""The GTK Photos tab tells a missing GdkPixbuf loader from a broken thumbnail.

Immich sends WebP thumbnails; without the WebP loader GdkPixbuf recognizes
none of them. The installed loaders are faked by patching
``supported_formats``, so the result does not depend on the host.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from gi.repository import Gio, GLib

from blueferry import photos_view
from blueferry.ui import photos

from .photo_fixtures import png
from .test_photo_thumbnails import webp_lossless


def _ftyp(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"


@pytest.mark.parametrize(("head", "expected"), [
    (webp_lossless(8, 8), "webp"),
    (_ftyp(b"avif"), "avif"),
    (_ftyp(b"heic"), "heif"),
    (_ftyp(b"mif1"), "heif"),
    (_ftyp(b"isom"), ""),  # an MP4, not an image
    (png(2, 2), ""),
    (b"RIFF\x00\x00\x00\x00WAVE", ""),
    (b"", ""),
])
def test_sniffed_format(head, expected):
    assert photos.sniffed_format(head) == expected


def test_missing_loader_only_for_a_format_without_a_loader(monkeypatch):
    monkeypatch.setattr(photos, "supported_formats", lambda: frozenset({"png", "jpeg"}))
    assert photos.missing_loader(webp_lossless(8, 8)) == "webp"
    assert photos.missing_loader(_ftyp(b"avif")) == "avif"
    assert photos.missing_loader(png(2, 2)) == ""  # a loader exists: the file is broken
    monkeypatch.setattr(photos, "supported_formats", lambda: frozenset({"png", "webp"}))
    assert photos.missing_loader(webp_lossless(8, 8)) == ""


def test_supported_formats_is_computed_once():
    formats = photos.supported_formats()
    assert "png" in formats and photos.supported_formats() is formats


def test_banner_text_names_the_package():
    assert "webp-pixbuf-loader" in photos.missing_loader_text("webp")
    assert "AVIF" in photos.missing_loader_text("avif")


def _load(path) -> tuple:
    results: list[tuple] = []
    photos.load_thumbnail(
        Gio.File.new_for_path(str(path)).get_uri(), Gio.Cancellable(),
        lambda texture, missing="": results.append((texture, missing)),
    )
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while not results:
        assert time.monotonic() < deadline, "the thumbnail loader never answered"
        context.iteration(False) or time.sleep(0.005)
    assert len(results) == 1
    return results[0]


def test_an_unreadable_webp_reports_the_missing_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "supported_formats", lambda: frozenset({"png", "jpeg"}))
    path = tmp_path / "thumb.webp"
    path.write_bytes(webp_lossless(8, 8))  # undecodable with or without the loader
    assert _load(path) == (None, "webp")


def test_a_broken_file_with_a_loader_is_no_loader_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "supported_formats", lambda: frozenset({"png", "webp"}))
    broken = tmp_path / "broken.webp"
    broken.write_bytes(webp_lossless(8, 8))
    assert _load(broken) == (None, "")
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    assert _load(garbage) == (None, "")


def test_an_oversized_webp_is_skipped_without_sniffing(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "supported_formats", lambda: frozenset({"png"}))
    path = tmp_path / "large.webp"
    path.write_bytes(webp_lossless(8, 8) + bytes(photos.MAX_THUMBNAIL_BYTES))
    assert _load(path) == (None, "")


def _load_within(path, seconds: float = 5) -> list[tuple]:
    """Like :func:`_load`, but a loader that hangs on a FIFO fails the test
    instead of leaving a thread blocked in ``open`` behind it."""
    results: list[tuple] = []
    photos.load_thumbnail(
        Gio.File.new_for_path(str(path)).get_uri(), Gio.Cancellable(),
        lambda texture, missing="": results.append((texture, missing)),
    )
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not results and time.monotonic() < deadline:
        context.iteration(False) or time.sleep(0.005)
    return results


def _release(fifo) -> None:
    """Let a reader blocked in ``open`` on ``fifo`` go (it then sees EOF)."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # nobody is waiting


def test_a_normal_thumbnail_is_read_off_the_gtk_thread_and_decoded(tmp_path, monkeypatch):
    path = tmp_path / "wide.png"
    path.write_bytes(png(600, 300))
    readers = []
    read = photos_view.read_thumbnail

    def recording(*args):
        readers.append(threading.current_thread())
        return read(*args)

    monkeypatch.setattr(photos_view, "read_thumbnail", recording)
    texture, missing = _load(path)
    assert missing == "" and texture is not None
    assert (texture.get_width(), texture.get_height()) == (256, 128)
    assert readers and threading.main_thread() not in readers


def test_a_thumbnail_swapped_for_a_fifo_after_the_check_is_not_waited_on(tmp_path):
    path = tmp_path / "thumb.png"
    path.write_bytes(png(8, 8))
    # The list's check passed; the file is replaced by a FIFO afterwards.
    assert photos_view.safe_thumbnail(path) == path
    path.unlink()
    os.mkfifo(path, 0o600)
    results = _load_within(path)
    if not results:
        _release(path)
        pytest.fail("the thumbnail loader blocked on a FIFO until a writer appeared")
    assert results == [(None, "")]


def test_a_canvas_over_the_pixel_cap_is_not_decoded(tmp_path, monkeypatch):
    path = tmp_path / "thumb.png"
    path.write_bytes(png(8, 8))
    monkeypatch.setattr(photos_view, "MAX_THUMBNAIL_PIXELS", 63)
    assert photos.decode_thumbnail(path.read_bytes()) == (None, "")
    assert _load(path) == (None, "")
