"""Toolkit-neutral state of the Photos tab (a plugin with ``photos``).

Shared by the Qt client (``qt/controller.py``) and, soon, the GTK client.
The list is loaded only while the tab is shown and forgotten when it is
hidden. Opening a photo looks the plugin up again on the worker and calls
it only if it is still the one the list came from, because a call would
start a disabled plugin again by D-Bus activation.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from blueferry import photos_view
from blueferry.client_tasks import TaskRunner
from blueferry.plugin_api.manifest import PluginManifest


class PhotosTabModel:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        open_runner: TaskRunner | None = None,
        url: Callable[[object], str],
        open_file: Callable[[object], None],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        """``url`` turns a local path into the client's file URL ("" for
        none); ``open_file`` shows a downloaded original. A failed download
        goes to the runner's default error handler. ``open_runner`` (default:
        ``runner``) fetches originals, so a client can show its busy state
        during a download but not while the list loads."""
        self._runner = runner
        self._open_runner = open_runner or runner
        self._url = url
        self._open_file = open_file
        self._on_change = on_change
        self._watched = False
        self._plugin: PluginManifest | None = None
        self._state: dict[str, Any] = photos_view.initial_state()

    @property
    def state(self) -> dict[str, Any]:
        """present, ready, loaded, hint and items (id, label, video, thumbnail, original)."""
        return self._state

    @property
    def plugin(self) -> PluginManifest | None:
        return self._plugin

    def watch(self, watched: bool) -> None:
        """The tab became visible (load) or hidden (forget the list)."""
        self._watched = bool(watched)
        if self._watched:
            self.refresh()
            return
        if self._state["items"]:
            self._state = {**self._state, "items": []}
            self._on_change()

    def plugins_changed(self) -> None:
        """A plugin was enabled, disabled, installed or removed: forget the
        photo plugin until the next load finds it again."""
        self._plugin = None
        self.refresh()

    def refresh(self) -> None:
        if not self._watched:
            return

        def completed(value: tuple[PluginManifest | None, photos_view.PhotosSnapshot]) -> None:
            manifest, snapshot = value
            self._plugin = manifest
            if not self._watched:
                return
            self._state = photos_view.loaded_state(snapshot, self._url)
            self._on_change()

        def failed(message: str) -> None:
            self._state = photos_view.failed_state(self._plugin is not None, message)
            self._on_change()

        self._runner.submit(photos_view.load_current, completed, failed)

    def open(self, photo_id: str) -> None:
        """Download the original (plugin) and open it in the default viewer."""
        manifest = self._plugin
        if manifest is None:
            return
        plugin_id = manifest.id

        def completed(value: object) -> None:
            if value is None:
                return
            self._state = photos_view.with_original(self._state, photo_id, self._url(value))
            self._on_change()
            self._open_file(value)

        self._open_runner.submit(
            lambda: photos_view.fetch_current_original(plugin_id, str(photo_id)), completed)
