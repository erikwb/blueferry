"""The Qt Photos tab reads thumbnails through its image provider, never by path.

QML's ``Image`` opens a ``file:`` URL itself, so a thumbnail swapped for a
FIFO after the list's check would block its loader. The provider reads the
bytes without waiting on a FIFO and decodes them with Qt's image reader.
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
pytest.importorskip("PySide6.QtQuick")

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine

from blueferry import photos_view
from blueferry.photos_view import read_thumbnail
from blueferry.plugin_api.client import Photo
from blueferry.plugin_api.testing import manifest
from blueferry.qt import thumbnails
from blueferry.qt.controller import BridgeController
from blueferry.qt.thumbnails import (
    THUMBNAIL_PROVIDER,
    ThumbnailImageProvider,
    decode_thumbnail,
    path_from_id,
    thumbnail_url,
)
from tests.client_fakes import InlineRunner
from tests.photo_fixtures import png
from tests.qt_settle import main_thread_gc
from tests.test_photo_thumbnails import png_bomb

pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)

# QML's Image as PhotosTab.qml declares it (the properties that load it).
_IMAGE = b"""
import QtQuick
Image {
    sourceSize.width: 256
    sourceSize.height: 256
    fillMode: Image.PreserveAspectCrop
    asynchronous: true
    cache: false
    readonly property int loadStatus: status  // the enum has no Python converter
}
"""


@pytest.fixture(scope="module")
def application():
    return QGuiApplication.instance() or QGuiApplication([])


def test_urls_round_trip_odd_paths() -> None:
    path = "/tmp/a b/%20?#x/café.png"
    url = thumbnail_url(path)
    assert url.startswith(f"image://{THUMBNAIL_PROVIDER}/") and "?" not in url
    assert path_from_id(url.split("/", 3)[3]) == path
    assert thumbnail_url(None) == ""


def test_decode_covers_the_requested_size(application) -> None:
    image = decode_thumbnail(png(600, 300), QSize(256, 256))
    assert (image.width(), image.height()) == (512, 256)
    small = decode_thumbnail(png(8, 8), QSize(256, 256))
    assert (small.width(), small.height()) == (8, 8)  # never scaled up
    assert decode_thumbnail(png_bomb(100_000, 100_000), QSize(256, 256)).isNull()
    assert decode_thumbnail(b"not an image").isNull()


def test_the_provider_reads_regular_files_only(application, tmp_path) -> None:
    provider = ThumbnailImageProvider()
    path = tmp_path / "t.png"
    path.write_bytes(png(4, 4))
    image_id = thumbnail_url(path).split("/", 3)[3]
    assert not provider.requestImage(image_id, QSize(), QSize(256, 256)).isNull()
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    fifo_id = thumbnail_url(fifo).split("/", 3)[3]
    assert provider.requestImage(fifo_id, QSize(), QSize(256, 256)).isNull()  # never blocks


@pytest.fixture
def engine(application):
    engine = QQmlEngine()
    # As blueferry.qt.app registers it for the real window.
    engine.addImageProvider(THUMBNAIL_PROVIDER, ThumbnailImageProvider())
    yield engine
    engine.deleteLater()
    QGuiApplication.processEvents()


def _thumbnail_source(monkeypatch, path) -> str:
    """The thumbnail URL the controller gives PhotosTab.qml for ``path``."""
    plugin = manifest("io.example.photos", capabilities="photos;")
    snapshot = photos_view.PhotosSnapshot(True, True, "1 recent item", [
        Photo("a", "2026-10-31T00:00:00Z", "image", path),
    ])
    monkeypatch.setattr(photos_view, "find_plugin", lambda: plugin)
    monkeypatch.setattr(photos_view, "load_recent", lambda _plugin: snapshot)
    controller = BridgeController(backend=SimpleNamespace(), setup=object(),
                                  subscribe=False, autostart=False)
    controller._photos._runner = controller._photos._open_runner = InlineRunner()
    controller.watchPhotos(True)
    source = controller.photos["items"][0]["thumbnail"]
    controller.watchPhotos(False)
    return source


def _image(engine, source: str, asynchronous: bool = True):
    component = QQmlComponent(engine)
    component.setData(_IMAGE, "")
    image = component.createWithInitialProperties(
        {"source": source, "asynchronous": asynchronous})
    assert image is not None, [e.toString() for e in component.errors()]
    image._component = component  # created objects die with their component
    return image


def _settled(image, seconds: float = 5) -> bool:
    """Process events until the image stops loading (``False`` on timeout)."""
    loading = 2  # Image.Loading
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QGuiApplication.processEvents()
        if image.property("loadStatus") != loading:
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("asynchronous", [True, False])
def test_a_listed_thumbnail_is_read_off_the_ui_thread_and_shown(
    engine, monkeypatch, tmp_path, asynchronous,
) -> None:
    path = tmp_path / "thumb.png"
    path.write_bytes(png(600, 300))
    readers = []

    def recording(*args):
        readers.append(threading.current_thread())
        return read_thumbnail(*args)

    monkeypatch.setattr(thumbnails, "read_thumbnail", recording)
    image = _image(engine, _thumbnail_source(monkeypatch, path), asynchronous)
    assert _settled(image)
    assert image.property("loadStatus") == 1  # Image.Ready
    assert image.property("implicitWidth") > 0 and image.property("implicitHeight") > 0
    assert readers and threading.main_thread() not in readers
    image.deleteLater()


def test_a_thumbnail_swapped_for_a_fifo_after_the_check_is_not_waited_on(
    engine, monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "thumb.png"
    path.write_bytes(png(8, 8))
    source = _thumbnail_source(monkeypatch, path)
    assert source  # the list's check passed
    path.unlink()
    os.mkfifo(path, 0o600)
    image = _image(engine, source)
    if not _settled(image):
        # Release the blocked open so the hang fails this test only.
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        _settled(image)
        image.deleteLater()
        pytest.fail("QML's image loader blocked on a FIFO until a writer appeared")
    assert image.property("loadStatus") == 3  # Image.Error: a placeholder
    image.deleteLater()
