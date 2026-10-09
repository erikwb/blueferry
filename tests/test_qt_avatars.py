"""Qt avatars: lazy controller cache, hardened decoding, and the QML component."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
pytest.importorskip("PySide6")

from PySide6.QtCore import (
    Property,
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QSize,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QTest

from blueferry.client import BackendError
from blueferry.qt import avatars
from blueferry.qt.avatars import (
    AVATAR_PROVIDER,
    AvatarImageProvider,
    address_from_id,
    avatar_url,
    decode_avatar,
)
from blueferry.qt.controller import BridgeController

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def application():
    return QGuiApplication.instance() or QGuiApplication([])


def _encoded(width: int, height: int, fmt: str) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x3366CC)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(data)


# ---- decoding ------------------------------------------------------------------

def test_decode_scales_png_and_jpeg_to_the_requested_size(application) -> None:
    png = decode_avatar(_encoded(300, 200, "PNG"), QSize(64, 64))
    assert (png.width(), png.height()) == (96, 64)
    jpeg = decode_avatar(_encoded(400, 400, "JPEG"))
    assert (jpeg.width(), jpeg.height()) == (128, 128)


@pytest.mark.parametrize("width,height", [(1, 2048), (2048, 1), (1, 300), (2048, 2048)])
def test_extreme_aspect_ratios_stay_within_the_decode_budget(application, width, height) -> None:
    image = decode_avatar(_encoded(width, height, "PNG"), QSize(128, 128))
    assert not image.isNull()
    assert image.width() <= 256 and image.height() <= 256
    assert image.sizeInBytes() <= avatars.MAX_AVATAR_DECODED_BYTES


def test_requested_size_is_clamped(application) -> None:
    image = decode_avatar(_encoded(2048, 2048, "PNG"), QSize(100_000, 100_000))
    assert image.width() <= avatars.MAX_AVATAR_SIZE * 2


def test_decode_rejects_bombs_foreign_formats_and_corrupt_data(application) -> None:
    from .photo_fixtures import png_header

    assert decode_avatar(_encoded(1, 4096, "PNG")).isNull()
    assert decode_avatar(_encoded(4096, 1, "PNG")).isNull()
    assert decode_avatar(png_header(30_000, 30_000)).isNull()
    assert decode_avatar(_encoded(8, 8, "BMP")).isNull()
    assert decode_avatar(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).isNull()
    assert decode_avatar(b"").isNull()


def test_provider_ids_round_trip_addresses_and_ignore_cache_busting(application) -> None:
    address = "+1 (555) 111/2222?#"
    url = avatar_url(address, 7)
    assert url.startswith(f"image://{AVATAR_PROVIDER}/")
    image_id = url.split("/", 3)[3]
    assert address_from_id(image_id) == address
    png = _encoded(10, 10, "PNG")
    provider = AvatarImageProvider({address: png}.get)
    assert not provider.requestImage(image_id, QSize(), QSize(10, 10)).isNull()
    assert provider.requestImage("unknown", QSize(), QSize(10, 10)).isNull()


# ---- controller cache ------------------------------------------------------------

class _Backend:
    def __init__(self, photos) -> None:
        self.photos = photos
        self.requests: list[str] = []

    def contact_photo(self, address):
        self.requests.append(address)
        value = self.photos.get(address, b"")
        if isinstance(value, Exception):
            raise value
        return value


def _controller(backend, *, enabled=True, revision=1):
    controller = BridgeController(backend=backend, setup=object(), subscribe=False, autostart=False)

    def run(operation, on_done=None, on_failed=None, *, busy=True):
        assert busy is False, "avatar fetches must never show the busy indicator"
        try:
            value = operation()
        except Exception as error:
            on_failed(str(error))
        else:
            on_done(value)

    controller._run = run
    controller._status = {"contact_photos": enabled, "contact_photo_revision": revision}
    controller._sync_avatars()
    return controller


def test_disabled_photos_never_fetch(application) -> None:
    backend = _Backend({"+15551112222": b"x"})
    controller = _controller(backend, enabled=False)
    assert controller.avatarSource("+15551112222") == ""
    assert backend.requests == []


def test_avatar_is_fetched_once_and_served_to_the_provider(application) -> None:
    png = _encoded(10, 10, "PNG")
    backend = _Backend({"+15551112222": png})
    controller = _controller(backend)
    changes = []
    controller.avatarsChanged.connect(lambda: changes.append(controller.avatarRevision))

    assert controller.avatarSource("+15551112222") == ""
    url = controller.avatarSource("+15551112222")
    assert url == avatar_url("+15551112222", controller._avatar_generation)
    assert controller.avatar_bytes("+15551112222") == png
    assert backend.requests == ["+15551112222"] and len(changes) == 1


def test_missing_lookups_are_quiet_until_the_generation_changes(application) -> None:
    backend = _Backend({})
    controller = _controller(backend)
    for _ in range(2):
        assert controller.avatarSource("+1") == ""
    assert backend.requests == ["+1"]
    assert controller.errorText == ""

    controller._status = {"contact_photos": True, "contact_photo_revision": 2}
    controller._sync_avatars()
    controller.avatarSource("+1")
    assert backend.requests == ["+1", "+1"]


def test_failed_lookups_back_off_and_retry(application) -> None:
    png = _encoded(10, 10, "PNG")
    backend = _Backend({"+2": BackendError("rate limited")})
    controller = _controller(backend)
    scheduled = []
    controller._schedule_avatar_retry = lambda delay, key, generation: scheduled.append(
        (delay, key, generation)
    )
    assert controller.avatarSource("+2") == ""
    assert controller.avatarSource("+2") == ""  # held back while backing off
    assert backend.requests == ["+2"] and controller.errorText == ""
    assert scheduled == [(30_000, "+2", controller._avatar_generation)]

    # Release as the timer would; a second failure doubles the delay.
    controller._avatar_backoff.pop("+2", None)
    controller.avatarSource("+2")
    assert scheduled[-1][0] == 60_000

    # Once the store answers, the avatar arrives and the failures reset.
    backend.photos["+2"] = png
    controller._avatar_backoff.pop("+2", None)
    controller.avatarSource("+2")
    assert controller.avatar_bytes("+2") == png and "+2" not in controller._avatar_failures


def _wait_until(predicate, timeout_ms: int) -> bool:
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    return bool(predicate())


def test_retry_timer_releases_the_address_and_asks_qml_again(application) -> None:
    controller = _controller(_Backend({}))
    controller._avatar_backoff["+9"] = None
    changes = []
    controller.avatarsChanged.connect(lambda: changes.append(True))
    controller._schedule_avatar_retry(1, "+9", controller._avatar_generation)
    # Poll with a generous deadline: the host may be heavily loaded.
    assert _wait_until(lambda: "+9" not in controller._avatar_backoff, 5_000)
    assert changes == [True]

    # A timer from an older generation is ignored; a current one set after it
    # fires later, so once it has run the stale one has run too.
    controller._avatar_backoff.update({"+8": None, "+7": None})
    controller._schedule_avatar_retry(1, "+8", controller._avatar_generation - 1)
    controller._schedule_avatar_retry(20, "+7", controller._avatar_generation)
    assert _wait_until(lambda: "+7" not in controller._avatar_backoff, 5_000)
    assert "+8" in controller._avatar_backoff


def test_revision_change_drops_cached_photos(application) -> None:
    png = _encoded(10, 10, "PNG")
    controller = _controller(_Backend({"+1": png}))
    controller.avatarSource("+1")
    assert controller.avatar_bytes("+1") == png
    controller._status = {"contact_photos": False}
    controller._sync_avatars()
    assert controller.avatar_bytes("+1") is None
    assert controller.avatarSource("+1") == ""


class _QueueWorker:
    """Serialized worker like the controller's single-thread pool."""

    def __init__(self) -> None:
        self.jobs: list = []

    def run(self, operation, on_done=None, on_failed=None, *, busy=True):
        self.jobs.append((operation, on_done, on_failed))

    def drain(self) -> None:
        while self.jobs:
            operation, on_done, on_failed = self.jobs.pop(0)
            try:
                value = operation()
            except Exception as error:
                on_failed(str(error))
            else:
                on_done(value)


def _queued_controller(photos):
    backend = _Backend(photos)
    controller = _controller(backend)
    worker = _QueueWorker()
    controller._run = worker.run
    return controller, backend, worker


def test_concurrent_fetches_all_land_with_one_call_each(application) -> None:
    png = _encoded(8, 8, "PNG")
    addresses = [f"+1555000{index:04d}" for index in range(20)]
    controller, backend, worker = _queued_controller({address: png for address in addresses})

    def binding_pass() -> list[str]:
        return [controller.avatarSource(address) for address in addresses]

    controller.avatarsChanged.connect(binding_pass)  # QML re-reads on every change
    assert binding_pass() == [""] * 20
    worker.drain()

    assert sorted(backend.requests) == sorted(addresses)  # N calls, not O(N^2)
    assert all(controller.avatar_bytes(address) == png for address in addresses)
    assert all(binding_pass())


def test_a_shown_avatars_url_is_stable_while_others_arrive(application) -> None:
    png = _encoded(8, 8, "PNG")
    controller, _backend, worker = _queued_controller({"+1": png, "+2": png, "+3": png})
    controller.avatarSource("+1")
    worker.drain()
    shown = controller.avatarSource("+1")
    assert shown
    controller.avatarSource("+2")
    controller.avatarSource("+3")
    worker.drain()
    assert controller.avatarSource("+1") == shown  # no reload of the shown image

    controller._status = {"contact_photos": True, "contact_photo_revision": 99}
    controller._sync_avatars()
    assert controller.avatarSource("+1") == ""  # new generation refetches


def test_avatar_cache_evicts_the_least_recently_used(application, monkeypatch) -> None:
    from blueferry.qt import controller as controller_module

    monkeypatch.setattr(controller_module, "MAX_CACHED_AVATARS", 2)
    png = _encoded(8, 8, "PNG")
    controller, backend, worker = _queued_controller({"+1": png, "+2": png, "+3": png})
    for address in ("+1", "+2"):
        controller.avatarSource(address)
    worker.drain()
    controller.avatarSource("+1")  # touch: +2 becomes least recently used
    controller.avatarSource("+3")
    worker.drain()
    assert controller.avatar_bytes("+2") is None
    assert controller.avatar_bytes("+1") == png and controller.avatar_bytes("+3") == png
    # Eviction is not a hard stop: an evicted address can be fetched again.
    controller.avatarSource("+2")
    worker.drain()
    assert backend.requests.count("+2") == 2


def test_avatar_cache_is_bounded_in_bytes(application, monkeypatch) -> None:
    from blueferry.qt import controller as controller_module

    png = _encoded(8, 8, "PNG")
    monkeypatch.setattr(controller_module, "MAX_CACHED_AVATAR_BYTES", 2 * len(png))
    controller, _backend, worker = _queued_controller({"+1": png, "+2": png, "+3": png})
    for address in ("+1", "+2", "+3"):
        controller.avatarSource(address)
        worker.drain()
    assert controller.avatar_bytes("+1") is None
    assert controller.avatar_bytes("+2") == png and controller.avatar_bytes("+3") == png
    # A single avatar above the byte bound is still kept (the newest one).
    monkeypatch.setattr(controller_module, "MAX_CACHED_AVATAR_BYTES", 1)
    controller.avatarSource("+1")
    worker.drain()
    assert controller.avatar_bytes("+1") == png
    assert controller.avatar_bytes("+2") is None and controller.avatar_bytes("+3") is None


def test_pending_fetches_are_bounded(application, monkeypatch) -> None:
    from blueferry.qt import controller as controller_module

    monkeypatch.setattr(controller_module, "MAX_PENDING_AVATARS", 2)
    backend = _Backend({})
    controller = _controller(backend)
    controller._run = lambda *_args, **_kwargs: None  # never completes
    for index in range(5):
        controller.avatarSource(f"+{index}")
    assert controller._avatar_pending == {"+0", "+1"}


# ---- QML component -----------------------------------------------------------------

class _Bridge(QObject):
    statusChanged = Signal()
    avatarsChanged = Signal()

    def __init__(self, enabled: bool) -> None:
        super().__init__()
        self._status = {"contact_photos": enabled}
        self.requested: list[str] = []

    @Property("QVariantMap", notify=statusChanged)
    def status(self):
        return self._status

    @Property(int, notify=avatarsChanged)
    def avatarRevision(self) -> int:
        return 1

    @Slot(str, result=str)
    def avatarSource(self, address: str) -> str:
        self.requested.append(address)
        return avatar_url(address, 1)


@pytest.mark.parametrize("enabled,group,expect_photo", [
    (False, False, False),
    (True, True, False),
    (True, False, True),
])
def test_contact_avatar_uses_the_photo_only_when_opted_in(
    application, enabled, group, expect_photo,
) -> None:
    engine = QQmlEngine()
    png = _encoded(32, 32, "PNG")
    engine.addImageProvider(AVATAR_PROVIDER, AvatarImageProvider({"+15551112222": png}.get))
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
    component = QQmlComponent(engine, QUrl.fromLocalFile(
        str(ROOT / "src/blueferry/qt/qml/ContactAvatar.qml")
    ))
    assert not component.isError(), [e.toString() for e in component.errors()]
    bridge = _Bridge(enabled)
    avatar = component.createWithInitialProperties({
        "bridge": bridge, "address": "+15551112222", "group": group,
    })
    assert avatar is not None, [e.toString() for e in component.errors()]
    for _ in range(100):
        QGuiApplication.processEvents()
        if avatar.property("photoReady") == expect_photo:
            break
        QTest.qWait(10)
    assert avatar.property("photoReady") is expect_photo
    assert bridge.requested == (["+15551112222"] if expect_photo else [])
    assert not warnings, warnings
    avatar.deleteLater()
    engine.deleteLater()
    QGuiApplication.processEvents()


def test_failure_bookkeeping_is_bounded(application, monkeypatch) -> None:
    from blueferry.qt import controller as controller_module

    monkeypatch.setattr(controller_module, "MAX_CACHED_AVATARS", 3)
    controller = _controller(_Backend({f"+{i}": BackendError("busy") for i in range(10)}))
    controller._schedule_avatar_retry = lambda *_args: None
    for index in range(10):
        controller.avatarSource(f"+{index}")
    assert list(controller._avatar_backoff) == ["+7", "+8", "+9"]
    assert list(controller._avatar_failures) == ["+7", "+8", "+9"]


@pytest.mark.parametrize('first_result', [b'', BackendError('busy')])
def test_empty_or_failed_fetches_release_slots_for_visible_avatars(
    application, monkeypatch, first_result,
) -> None:
    from blueferry.qt import controller as controller_module

    monkeypatch.setattr(controller_module, 'MAX_PENDING_AVATARS', 2)
    png = _encoded(8, 8, 'PNG')
    controller, backend, worker = _queued_controller({
        '+1': first_result, '+2': first_result, '+3': png,
    })
    controller._schedule_avatar_retry = lambda *_args: None

    def binding_pass():
        return [controller.avatarSource(address) for address in ('+1', '+2', '+3')]

    controller.avatarsChanged.connect(binding_pass)
    binding_pass()
    assert len(worker.jobs) == 2
    worker.drain()

    assert backend.requests == ['+1', '+2', '+3']
    assert controller.avatar_bytes('+3') == png
    assert binding_pass()[2]


@pytest.mark.parametrize('failed', [False, True])
def test_contact_refresh_retries_visible_avatar_after_stale_completion(application, failed):
    png = _encoded(8, 8, 'PNG')
    controller, backend, worker = _queued_controller({'+1': png})
    controller.avatarsChanged.connect(lambda: controller.avatarSource('+1'))
    controller.avatarSource('+1')
    operation, done, on_failed = worker.jobs.pop(0)
    controller._status['contact_photo_revision'] = 2
    controller._sync_avatars()
    assert worker.jobs == []

    if failed:
        on_failed('old failure')
    else:
        done(operation())
    worker.drain()

    assert controller.avatar_bytes('+1') == png
    assert controller.avatarSource('+1')
    assert len(backend.requests) == (1 if failed else 2)
