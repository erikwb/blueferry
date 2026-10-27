"""The GTK Photos tab tells a missing GdkPixbuf loader from a broken thumbnail.

Immich sends WebP thumbnails; without the WebP loader GdkPixbuf recognizes
none of them. The installed loaders are faked by patching
``supported_formats``, so the result does not depend on the host.
"""
from __future__ import annotations

import time

import pytest
from gi.repository import Gio, GLib

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
