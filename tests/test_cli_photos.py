"""`blueferry photos` and photos_view with scripted plugins; nothing opens."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from blueferry import cli_photos, photos_view
from blueferry.cli import app
from blueferry.plugin_api.client import Photo, PluginClient, PluginError
from blueferry.plugin_api.manifest import Discovery
from blueferry.plugin_api.testing import ScriptedTransport, manifest


def _factory(replies, cache):
    return lambda plugin: PluginClient(
        plugin, transport=ScriptedTransport(replies), cache_root=lambda: cache,
    )


def test_find_plugin_picks_a_photos_capability() -> None:
    other = manifest("io.example.chat", capabilities="conversations;")
    photos = manifest("io.example.photos")
    assert photos_view.find_plugin(Discovery((other, photos))) is photos
    assert photos_view.find_plugin(Discovery((other,))) is None
    assert photos_view.find_plugin(Discovery((photos,)), frozenset({photos.id})) is None


def test_load_recent_covers_missing_unconfigured_failing_and_ready(tmp_path) -> None:
    assert "blueferry plugins install https://" in photos_view.load_recent(None).hint
    unconfigured = photos_view.load_recent(manifest(), client_factory=_factory(
        {"Status": json.dumps({"state": "unconfigured", "detail": "run setup"})}, tmp_path))
    assert unconfigured.present and not unconfigured.ready and unconfigured.hint == "run setup"
    failing = photos_view.load_recent(manifest(), client_factory=_factory(
        {"Status": '{"state": "ok"}', "ListRecent": PluginError("the plugin did not answer")},
        tmp_path))
    assert "did not answer" in failing.hint
    ready = photos_view.load_recent(manifest(), 5, client_factory=_factory({
        "Status": json.dumps({"state": "ok", "server": "photos.example.org"}),
        "ListRecent": json.dumps([{"id": "a", "taken_at": "2026-10-06T16:21:00Z",
                                   "type": "video"}]),
    }, tmp_path))
    assert ready.ready and ready.hint == "1 recent items from photos.example.org"
    assert photos_view.type_text(ready.photos[0]) == "Video"


def test_labels_use_local_time(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Zurich")
    import time
    time.tzset()
    try:
        photo = Photo("a", "2026-10-06T16:21:00.000Z", "image", None)
        assert photos_view.label(photo) == "2026-10-06 18:21  Photo"
        assert photos_view.label(Photo("b", "", "other", None)) == "File"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


@pytest.fixture
def hooks(monkeypatch, tmp_path):
    opened: list[str] = []
    original = tmp_path / "IMG_0001.HEIC"
    original.write_bytes(b"x")
    state = {"plugin": manifest()}
    monkeypatch.setitem(cli_photos._hooks, "plugin", lambda: state["plugin"])
    monkeypatch.setitem(cli_photos._hooks, "open_uri", opened.append)
    monkeypatch.setitem(cli_photos._hooks, "fetch", lambda _m, photo_id: (
        original if photo_id == "a" else (_ for _ in ()).throw(PluginError("not found"))))
    monkeypatch.setitem(cli_photos._hooks, "load", lambda _m, limit: photos_view.PhotosSnapshot(
        True, True, "", [Photo("a", "2026-10-06T16:21:00Z", "image", None)][:limit]))
    return state, opened, original


def test_photos_recent_and_open(hooks) -> None:
    state, opened, original = hooks
    result = CliRunner().invoke(app, ["photos", "recent", "--limit", "5"])
    assert result.exit_code == 0 and result.output.startswith("a  2026-10-06")
    result = CliRunner().invoke(app, ["photos", "open", "a"])
    assert result.exit_code == 0 and opened == [original.as_uri()]
    result = CliRunner().invoke(app, ["photos", "open", "a", "--print-path"])
    assert result.output.strip() == os.fspath(original) and len(opened) == 1
    result = CliRunner().invoke(app, ["photos", "open", "zzz"])
    assert result.exit_code == 1 and "not found" in result.output
    state["plugin"] = None
    result = CliRunner().invoke(app, ["photos", "recent"])
    assert result.exit_code == 2 and "blueferry plugins install" in result.output
