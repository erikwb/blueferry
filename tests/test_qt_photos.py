"""Slow photo plugins must not block the backend or restore stale content."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QDesktopServices, QGuiApplication

from blueferry import photos_view
from blueferry.plugin_api.client import Photo
from blueferry.plugin_api.testing import manifest
from blueferry.qt.controller import BridgeController
from tests.qt_settle import main_thread_gc, wait

pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QGuiApplication([])


def snapshot(photo_id="1"):
    return photos_view.PhotosSnapshot(True, True, "ready", [
        Photo(photo_id, "2026-10-31T00:00:00Z", "image", None),
    ])


@pytest.fixture
def controller(app, monkeypatch):
    plugin = manifest("io.example.photos", capabilities="photos;")
    monkeypatch.setattr(photos_view, "find_plugin", lambda: plugin)
    monkeypatch.setattr(photos_view, "load_recent", lambda _plugin: snapshot())
    backend = SimpleNamespace(marked=threading.Event())
    backend.mark_thread_read = lambda _key: backend.marked.set()
    controller = BridgeController(backend=backend, setup=object(),
                                  subscribe=False, autostart=False)
    monkeypatch.setattr(controller._plugin_surfaces, "schedule_reload", lambda: None)
    yield controller
    controller.watchPhotos(False)
    for _ in range(3):
        assert controller._photos_pool.waitForDone(2000)
        assert controller._pool.waitForDone(2000)
        QCoreApplication.processEvents()


@dataclass
class Gate:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def block(self):
        self.entered.set()
        assert self.release.wait(5), "Test did not release the fake plugin"


@pytest.fixture
def gates(controller):
    gates = []

    def create():
        gate = Gate()
        gates.append(gate)
        return gate

    yield create
    for gate in gates:
        gate.release.set()


def loaded(controller):
    return controller.photos["loaded"] and not controller.photos["busy"]


@pytest.mark.parametrize("operation", ["list", "fetch"])
def test_slow_photo_plugin_leaves_backend_and_global_busy_free(
    controller, gates, monkeypatch, tmp_path, operation,
):
    gate = gates()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))

    def block(*_args):
        gate.block()
        return snapshot() if operation == "list" else tmp_path / "original.jpg"

    if operation == "list":
        monkeypatch.setattr(photos_view, "load_recent", block)
        controller.watchPhotos(True)
    else:
        controller.watchPhotos(True)
        wait(lambda: loaded(controller))
        monkeypatch.setattr(photos_view, "fetch_original", block)
        controller.openPhoto("1")
    wait(gate.entered.is_set)
    assert controller.photos["busy"] and not controller.busy
    controller.markThreadRead("thread-1")
    wait(controller._backend.marked.is_set, timeout_ms=1000)
    assert not gate.release.is_set()  # backend finished while plugin is still waiting
    gate.release.set()
    wait(lambda: loaded(controller))
    assert len(opened) == (1 if operation == "fetch" else 0)


@pytest.mark.parametrize("operation", ["list", "fetch"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("invalidation", ["hide", "switch-hidden", "switch-visible"])
def test_stale_photo_replies_are_discarded_after_hiding_or_switching(
    controller, gates, monkeypatch, tmp_path, operation, fails, invalidation,
):
    old_gate, new_gate = gates(), gates()
    old_plugin = photos_view.find_plugin()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))

    def old_result(*_args):
        old_gate.block()
        if fails:
            raise RuntimeError("stale plugin error")
        return snapshot("old") if operation == "list" else tmp_path / "old.jpg"

    if operation == "list":
        monkeypatch.setattr(photos_view, "load_recent", old_result)
        controller.watchPhotos(True)
    else:
        controller.watchPhotos(True)
        wait(lambda: loaded(controller))
        monkeypatch.setattr(photos_view, "fetch_original", old_result)
        controller.openPhoto("1")
    wait(old_gate.entered.is_set)
    if invalidation != "switch-visible":
        controller.watchPhotos(False)
    if invalidation != "hide":
        new_plugin = manifest("io.example.new", capabilities="photos;")
        monkeypatch.setattr(photos_view, "find_plugin", lambda: new_plugin)
        controller._plugin_set = ((old_plugin.id, True),)
        monkeypatch.setattr(controller._plugin_settings, "state", lambda: {
            "loaded": True, "plugins": [{"id": old_plugin.id, "enabled": False},
                                        {"id": new_plugin.id, "enabled": True}],
        })
        controller._plugin_set_changed()

    def fresh_result(_plugin):
        new_gate.block()
        return snapshot("fresh")

    monkeypatch.setattr(photos_view, "load_recent", fresh_result)
    controller.watchPhotos(True)
    old_gate.release.set()
    wait(new_gate.entered.is_set)
    assert controller.photos["items"] == [] and controller._photos_plugin is None
    assert controller.errorText == "" and opened == []
    assert controller.photos["hint"] == "" and not controller.photos["loaded"]
    new_gate.release.set()
    wait(lambda: loaded(controller))
    assert [item["id"] for item in controller.photos["items"]] == ["fresh"]


def test_download_serializes_clicks_and_coalesces_refreshes(
    controller, gates, monkeypatch, tmp_path,
):
    gate = gates()
    lists, downloads, opened = [], [], []
    monkeypatch.setattr(photos_view, "load_recent", lambda plugin: lists.append(plugin) or snapshot())
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))

    def download(_plugin, photo_id):
        downloads.append(photo_id)
        gate.block()
        return tmp_path / "original.jpg"

    monkeypatch.setattr(photos_view, "fetch_original", download)
    controller.watchPhotos(True)
    wait(lambda: loaded(controller))
    controller.openPhoto("1")
    wait(gate.entered.is_set)
    for _ in range(30):
        controller.openPhoto("1")
        controller.refreshPhotos()
    gate.release.set()
    wait(lambda: not controller._tasks)
    assert downloads == ["1"] and len(lists) == 2 and len(opened) == 1
    assert loaded(controller)


def test_failed_download_releases_photo_busy_and_can_be_retried(controller, monkeypatch, tmp_path):
    controller.watchPhotos(True)
    wait(lambda: loaded(controller))

    def fail(*_args):
        raise RuntimeError("download failed")

    monkeypatch.setattr(photos_view, "fetch_original", fail)
    controller.openPhoto("1")
    wait(lambda: not controller._tasks)
    assert loaded(controller) and not controller.busy
    assert controller.errorText == "download failed"
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    monkeypatch.setattr(photos_view, "fetch_original", lambda *_args: tmp_path / "retry.jpg")
    controller.openPhoto("1")
    wait(lambda: not controller._tasks)
    assert loaded(controller)
    assert [url.toLocalFile() for url in opened] == [str(tmp_path / "retry.jpg")]
