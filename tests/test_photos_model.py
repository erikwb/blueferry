"""The Photos tab state shared by the clients: no toolkit, no plugin started."""
from __future__ import annotations

from blueferry import photos_view
from blueferry.photos_model import PhotosTabModel
from blueferry.plugin_api.client import Photo
from blueferry.plugin_api.testing import manifest
from tests.client_fakes import ManualRunner
from tests.photo_fixtures import png
from tests.test_photo_thumbnails import png_bomb


def _url(path: object) -> str:
    return f"file://{path}" if path else ""


def _model(work: ManualRunner):
    opened: list[object] = []
    changes: list[None] = []
    model = PhotosTabModel(runner=work, url=_url, open_file=opened.append,
                           on_change=lambda: changes.append(None))
    return model, opened, changes


def test_loads_only_while_watched_and_opens_the_original(monkeypatch, tmp_path) -> None:
    plugin = manifest()
    thumb = tmp_path / "t.webp"
    thumb.write_bytes(png(2, 2))
    original = tmp_path / "IMG_1.HEIC"
    snapshot = photos_view.PhotosSnapshot(True, True, "1 recent items", [
        Photo("a", "2026-10-06T16:21:00Z", "video", thumb),
    ])
    monkeypatch.setattr(photos_view, "find_plugin", lambda: plugin)
    monkeypatch.setattr(photos_view, "load_recent", lambda manifest: snapshot)
    fetched: list[str] = []
    monkeypatch.setattr(photos_view, "fetch_original",
                        lambda manifest, photo_id: fetched.append(photo_id) or original)
    work = ManualRunner()
    model, opened, changes = _model(work)
    model.refresh()
    assert not work.pending and model.state["loaded"] is False  # not watched
    model.open("a")  # no list, no plugin: nothing to fetch
    assert not work.pending
    model.watch(True)
    work.run_tasks()
    item = model.state["items"][0]
    assert item == {"id": "a", "label": photos_view.label(snapshot.photos[0]), "video": True,
                    "thumbnail": f"file://{thumb}", "original": ""}
    assert model.state["ready"] is True and model.plugin is plugin and changes
    model.open("a")
    assert fetched == []  # on the worker
    work.run_tasks()
    assert fetched == ["a"] and opened == [original]
    assert model.state["items"][0]["original"] == f"file://{original}"
    model.watch(False)
    assert model.state["items"] == []


def test_oversized_thumbnails_are_checked_on_the_worker(monkeypatch, tmp_path) -> None:
    good = tmp_path / "good.png"
    good.write_bytes(png(2, 2))
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(png_bomb(100_000, 100_000))
    big = tmp_path / "big.png"
    big.write_bytes(png(2, 2) + bytes(photos_view.MAX_THUMBNAIL_BYTES))
    snapshot = photos_view.PhotosSnapshot(True, True, "3 recent items", [
        Photo("a", "", "image", good), Photo("b", "", "image", bomb),
        Photo("c", "", "image", big),
    ])
    monkeypatch.setattr(photos_view, "find_plugin", manifest)
    monkeypatch.setattr(photos_view, "load_recent", lambda manifest: snapshot)
    checked: list[object] = []
    safe = photos_view.safe_thumbnail
    monkeypatch.setattr(photos_view, "safe_thumbnail",
                        lambda path: checked.append(path) or safe(path))
    work = ManualRunner()
    model, _opened, _changes = _model(work)
    model.watch(True)
    assert checked == []  # nothing read before the worker runs
    work.run_tasks()
    assert checked == [good, bomb, big]
    assert [item["thumbnail"] for item in model.state["items"]] == [f"file://{good}", "", ""]


def test_a_photo_of_a_plugin_disabled_or_replaced_meanwhile_is_not_fetched(
    monkeypatch, tmp_path,
) -> None:
    plugin = manifest()
    found: list[object] = [plugin]
    snapshot = photos_view.PhotosSnapshot(True, True, "1 recent items", [
        Photo("a", "2026-10-06T16:21:00Z", "photo", tmp_path / "t.webp"),
    ])
    monkeypatch.setattr(photos_view, "find_plugin", lambda: found[0])
    monkeypatch.setattr(photos_view, "load_recent", lambda manifest: snapshot)
    fetched: list[str] = []
    monkeypatch.setattr(photos_view, "fetch_original",
                        lambda manifest, photo_id: fetched.append(photo_id) or tmp_path / "a")
    work = ManualRunner()
    model, opened, _changes = _model(work)
    model.watch(True)
    work.run_tasks()
    found[0] = None  # disabled after the list loaded
    model.open("a")
    work.run_tasks()
    found[0] = manifest("io.example.other")  # another photo plugin now
    model.open("a")
    work.run_tasks()
    assert fetched == [] and opened == []

    monkeypatch.setattr(photos_view, "load_recent",
                        lambda manifest: photos_view.PhotosSnapshot(False, False, "none", []))
    found[0] = None
    model.plugins_changed()
    assert model.plugin is None
    work.run_tasks()
    assert model.state["items"] == [] and model.state["hint"] == "none"


def test_a_failed_load_stops_the_spinner_and_a_late_load_is_dropped(monkeypatch) -> None:
    def boom():
        raise RuntimeError("")

    monkeypatch.setattr(photos_view, "find_plugin", boom)
    work = ManualRunner()
    model, _opened, _changes = _model(work)
    model.watch(True)
    work.run_tasks()
    assert model.state == {"present": False, "ready": False, "loaded": True,
                           "hint": "Photos unavailable", "items": []}

    plugin = manifest()
    monkeypatch.setattr(photos_view, "find_plugin", lambda: plugin)
    monkeypatch.setattr(photos_view, "load_recent",
                        lambda manifest: photos_view.PhotosSnapshot(True, True, "x", []))
    model.refresh()
    model.watch(False)  # hidden before the answer
    work.run_tasks()
    assert model.plugin is plugin and model.state["hint"] == "Photos unavailable"


def test_originals_are_fetched_on_the_open_runner(monkeypatch, tmp_path) -> None:
    plugin = manifest()
    snapshot = photos_view.PhotosSnapshot(True, True, "1 recent items", [
        Photo("a", "2026-10-06T16:21:00Z", "photo", tmp_path / "t.webp"),
    ])
    monkeypatch.setattr(photos_view, "find_plugin", lambda: plugin)
    monkeypatch.setattr(photos_view, "load_recent", lambda manifest: snapshot)
    monkeypatch.setattr(photos_view, "fetch_original",
                        lambda manifest, photo_id: tmp_path / "IMG_1.HEIC")
    work, downloads = ManualRunner(), ManualRunner()
    opened: list[object] = []
    model = PhotosTabModel(runner=work, open_runner=downloads, url=_url,
                           open_file=opened.append)
    model.watch(True)
    work.run_tasks()
    model.open("a")
    assert not work.pending and downloads.pending
    downloads.run_tasks()
    assert opened == [tmp_path / "IMG_1.HEIC"]
