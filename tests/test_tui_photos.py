"""Photos from a photo plugin in the Textual client (fakes only)."""
from __future__ import annotations

from types import SimpleNamespace

from textual.app import App
from textual.widgets import OptionList, Static

from blueferry import photos_view
from blueferry.photos_model import PhotosTabModel
from blueferry.plugin_api.client import Photo
from blueferry.tui_photos import PhotosScreen, file_url, shown_items, url_path
from tests.client_fakes import ManualRunner
from tests.test_tui import _run_headless


class _Host(App[None]):
    CSS_PATH = "../src/blueferry/tui.tcss"


def test_shown_items_and_file_urls(tmp_path) -> None:
    items = [{"id": "a", "video": False}, {"id": "b", "video": True}]
    assert [i["id"] for i in shown_items(items, "all")] == ["a", "b"]
    assert [i["id"] for i in shown_items(items, "photos")] == ["a"]
    assert [i["id"] for i in shown_items(items, "videos")] == ["b"]
    path = tmp_path / "a b.jpg"
    assert url_path(file_url(path)) == str(path)
    assert file_url(None) == "" and url_path("https://x.example/a") == ""


def test_photos_list_opens_and_copies_the_original(tmp_path, monkeypatch) -> None:
    original = tmp_path / "IMG 1.jpg"
    original.write_bytes(b"x")
    snapshot = photos_view.PhotosSnapshot(True, True, "", [
        Photo("p1", "2026-10-01T10:00:00Z", "photo", None),
        Photo("v1", "2026-10-01T09:00:00Z", "video", None),
    ])
    plugin = SimpleNamespace(id="io.example.photos")
    monkeypatch.setattr(photos_view, "load_current", lambda: (plugin, snapshot))
    fetched: list[tuple] = []

    def fetch(plugin_id, photo_id):
        fetched.append((plugin_id, photo_id))
        return original

    monkeypatch.setattr(photos_view, "fetch_current_original", fetch)
    work = ManualRunner()
    opened: list[str] = []

    def make(on_change, open_file) -> PhotosTabModel:
        model = PhotosTabModel(runner=work, url=file_url, open_file=open_file,
                               on_change=on_change)
        return model

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 40)) as pilot:
            screen = PhotosScreen(make, opener=opened.append)
            await app.push_screen(screen)
            work.run_tasks()
            await pilot.pause()
            listing = screen.query_one("#photos-list", OptionList)
            assert listing.option_count == 2
            assert "Video" in listing.get_option_at_index(1).prompt.plain

            await pilot.press("c")  # not downloaded yet
            await pilot.press("f")
            await pilot.pause()
            assert listing.option_count == 1  # photos only
            assert "[Photos]" in screen.query_one("#photos-filter", Static).render().plain

            listing.focus()
            await pilot.press("enter")
            work.run_tasks()
            await pilot.pause()
            assert fetched == [("io.example.photos", "p1")]
            assert opened == [original.resolve().as_uri()]
            assert listing.get_option_at_index(0).prompt.plain.endswith("✓")
            copied: list[str] = []
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
            await pilot.press("c")
            assert copied == [str(original.resolve())]
            await pilot.press("escape")
            await pilot.pause()
            assert screen.model.state["items"] == []  # forgotten when closed

    _run_headless(scenario())


def test_a_failed_download_is_reported(monkeypatch) -> None:
    """The default factory wires the model's on_error to a notice.

    The screen builds its own model here; only the open runner is slowed
    down to the test's manual one so the failure lands deterministically.
    """
    snapshot = photos_view.PhotosSnapshot(True, True, "", [
        Photo("p1", "2026-10-01T10:00:00Z", "photo", None),
    ])
    plugin = SimpleNamespace(id="io.example.photos")
    monkeypatch.setattr(photos_view, "load_current", lambda: (plugin, snapshot))

    def fetch(_plugin_id, _photo_id):
        raise RuntimeError("download [b]broke[/b]")

    monkeypatch.setattr(photos_view, "fetch_current_original", fetch)
    notices: list[str] = []

    async def until(pilot, condition) -> None:
        for _attempt in range(60):
            if condition():
                return
            await pilot.pause(0.05)
        assert condition()

    async def scenario() -> None:
        app = _Host()
        async with app.run_test(size=(100, 40)) as pilot:
            screen = PhotosScreen(opener=lambda _uri: None)
            await app.push_screen(screen)
            await until(pilot, lambda: bool(screen.items))
            screen.notify = lambda message, **_kwargs: notices.append(message)  # type: ignore[method-assign]
            work = ManualRunner()
            assert screen.model is not None
            screen.model._open_runner = work  # the manual runner, deterministically
            listing = screen.query_one("#photos-list", OptionList)
            listing.focus()
            await pilot.press("enter")
            work.run_tasks()
            await pilot.pause()
            assert notices and "download" in notices[-1] and "\x1b" not in notices[-1]

    _run_headless(scenario())
