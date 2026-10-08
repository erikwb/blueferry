"""GTK avatar decoding, asynchronous cache lifetime, and group fallbacks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry.models import Thread
from blueferry.ui import avatars, conversations


def _png(width=320, height=160):
    pixels = avatars.GdkPixbuf.Pixbuf.new(
        avatars.GdkPixbuf.Colorspace.RGB, False, 8, width, height,
    )
    pixels.fill(0x3366CCFF)
    success, data = pixels.save_to_bufferv("png", [], [])
    assert success
    return bytes(data)


@pytest.mark.parametrize("width,height", [(320, 160), (1, 2048), (2048, 1)])
def test_gtk_decodes_a_bounded_thumbnail(width, height):
    texture = avatars.decode_avatar(_png(width, height))
    assert texture is not None
    assert 0 < texture.get_width() <= avatars.AVATAR_SIZE
    assert 0 < texture.get_height() <= avatars.AVATAR_SIZE


def test_gtk_rejects_invalid_and_oversized_canvases():
    from .photo_fixtures import png_header

    assert avatars.decode_avatar(b"GIF89a") is None
    assert avatars.decode_avatar(png_header(30000, 30000)) is None
    assert avatars.decode_avatar(png_header(32, 32)) is None


def _cache():
    reads, changes = [], []
    clock = [0.0]
    cache = avatars.AvatarCache(
        lambda *args: reads.append(args), lambda: changes.append(True), clock=lambda: clock[0],
    )
    return cache, reads, changes, clock


def test_gtk_photo_reads_are_opt_in_and_shared_by_list_and_header():
    cache, reads, _changes, _clock = _cache()
    assert cache.get("alice@example.com") is None
    assert reads == []
    cache.update_status({"contact_photos": True, "contact_photo_revision": 1})
    assert cache.get("alice@example.com") is None
    assert cache.get("alice@example.com") is None
    assert len(reads) == 1
    texture = avatars.decode_avatar(_png())
    reads[0][1](texture)
    assert cache.get("alice@example.com") is texture
    assert len(reads) == 1


@pytest.mark.parametrize("next_status", [
    {"contact_photos": False},
    {"contact_photos": True, "contact_photo_revision": 2},
    {},
])
def test_gtk_discards_late_photos_after_disable_refresh_or_backend_loss(next_status):
    cache, reads, _changes, _clock = _cache()
    cache.update_status({"contact_photos": True, "contact_photo_revision": 1})
    cache.get("alice@example.com")
    cache.update_status(next_status)
    reads[0][1](object())
    assert cache.get("alice@example.com") is None
    assert len(reads) == (2 if next_status.get("contact_photos") else 1)


def test_gtk_missing_photos_are_cached_and_failures_back_off():
    cache, reads, _changes, clock = _cache()
    cache.update_status({"contact_photos": True})
    cache.get("missing@example.com")
    reads[-1][1](None)
    cache.get("missing@example.com")
    assert len(reads) == 1
    cache.get("retry@example.com")
    reads[-1][2]("busy")
    cache.get("retry@example.com")
    assert len(reads) == 2
    clock[0] = 30
    cache.get("retry@example.com")
    assert len(reads) == 3
    reads[-1][2]("busy")
    clock[0] = 89
    cache.get("retry@example.com")
    assert len(reads) == 3
    clock[0] = 90
    cache.get("retry@example.com")
    assert len(reads) == 4


def test_gtk_bounds_in_flight_reads_even_during_repeated_refreshes(monkeypatch):
    monkeypatch.setattr(avatars, "MAX_PENDING_AVATARS", 2)
    cache, reads, _changes, _clock = _cache()
    for revision in range(5):
        cache.update_status({"contact_photos": True, "contact_photo_revision": revision})
        for address in ["one@example.com", "two@example.com", "three@example.com"]:
            cache.get(address)
    assert len(reads) == 2
    reads[0][1](object())
    cache.get("three@example.com")
    assert len(reads) == 3


def test_gtk_bounds_cache_without_repeatedly_refetching_visible_avatars(monkeypatch):
    monkeypatch.setattr(avatars, "MAX_AVATARS", 2)
    cache, reads, _changes, _clock = _cache()
    cache.update_status({"contact_photos": True})
    for address in ["one@example.com", "two@example.com"]:
        cache.get(address)
        reads[-1][1](object())
    for _ in range(5):
        for address in ["one@example.com", "two@example.com", "three@example.com"]:
            cache.get(address)
    assert len(reads) == 2


def test_gtk_group_icons_never_use_a_participants_photo():
    lookups = []
    icons = []
    page = SimpleNamespace(_avatars=SimpleNamespace(get=lambda address: lookups.append(address)))
    image = SimpleNamespace(
        set_visible=lambda _visible: None, set_from_icon_name=icons.append,
    )
    group = Thread.from_dict({"is_group": True, "recipients": ["alice@example.com"]})
    conversations.ConversationsPage._set_avatar(page, image, group)
    assert lookups == [""]
    assert icons == ["system-users-symbolic"]
