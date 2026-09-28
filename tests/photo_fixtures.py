"""Tiny, structurally valid JPEG/PNG byte strings for contact-photo tests."""
from __future__ import annotations

import struct
import zlib


def png(width: int = 2, height: int = 2) -> bytes:
    """A real PNG (decodable by Qt) with the given canvas."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def png_header(width: int, height: int) -> bytes:
    """Only a PNG signature and IHDR: declares a canvas without pixel data."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    )


def jpeg(width: int = 3, height: int = 2, *, sof: int = 0xC0, padding: int = 768) -> bytes:
    """A JPEG marker stream (SOI, APP0, SOFn, EOI) declaring ``width``x``height``.

    It is not decodable pixel data; it exercises header validation only.
    """
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
        + b"\xff" + bytes([sof]) + struct.pack(">H", len(frame) + 2) + frame
        + bytes(padding)  # stands in for scan data; never parsed
        + b"\xff\xd9"
    )
