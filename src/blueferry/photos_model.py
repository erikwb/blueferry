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
        on_error: Callable[[str], None] = lambda _message: None,
    ) -> None:
        """``url`` turns a local path into the client's file URL ("" for
        none); ``open_file`` shows a downloaded original. Both runners must
        be independent of the client's backend queue. The model serializes
        work and exposes its own busy state for both lists and originals.
        Only errors belonging to the current tab reach ``on_error``."""
        self._runner = runner
        self._open_runner = open_runner or runner
        self._url = url
        self._open_file = open_file
        self._on_change = on_change
        self._on_error = on_error
        self._working = False
        self._again = False
        self._generation = 0
        self._watched = False
        self._plugin: PluginManifest | None = None
        self._state: dict[str, Any] = {**photos_view.initial_state(), "busy": False}

    @property
    def state(self) -> dict[str, Any]:
        """present, ready, loaded, hint and items (id, label, video, thumbnail, original)."""
        return self._state

    @property
    def plugin(self) -> PluginManifest | None:
        return self._plugin

    def watch(self, watched: bool) -> None:
        """The tab became visible (load) or hidden (forget the list)."""
        if self._watched == bool(watched):
            return
        self._watched = bool(watched)
        if self._watched:
            self.refresh()
            return
        self._clear()

    def _clear(self) -> None:
        self._generation += 1
        self._again = False
        self._plugin = None
        self._state = {**photos_view.initial_state(), "busy": self._working}
        self._on_change()

    def _current(self, generation: int) -> bool:
        return self._watched and generation == self._generation

    def _run(
        self, runner: TaskRunner, operation: Callable[[], object],
        completed: Callable[[Any], None], failed: Callable[[str], None],
    ) -> None:
        self._working = True
        self._state = {**self._state, "busy": True}
        self._on_change()

        def finished() -> None:
            self._working = False
            self._state = {**self._state, "busy": False}
            self._on_change()
            if self._again:
                self._again = False
                self.refresh()

        runner.submit(operation, completed, failed, finished)

    def plugins_changed(self) -> None:
        """A plugin was enabled, disabled, installed or removed: forget the
        photo plugin until the next load finds it again."""
        self._clear()
        self.refresh()

    def refresh(self) -> None:
        if not self._watched:
            return
        if self._working:
            self._again = True
            return
        generation = self._generation

        def load():
            if not self._current(generation):
                return None
            return photos_view.load_current()

        def completed(value: Any) -> None:
            if not self._current(generation) or value is None:
                return
            manifest, snapshot = value
            self._plugin = manifest
            self._state = {**photos_view.loaded_state(snapshot, self._url), "busy": self._working}
            self._on_change()

        def failed(message: str) -> None:
            if not self._current(generation):
                return
            self._state = {**photos_view.failed_state(self._plugin is not None, message),
                           "busy": self._working}
            self._on_change()

        self._run(self._runner, load, completed, failed)

    def open(self, photo_id: str) -> None:
        """Download the original (plugin) and open it in the default viewer."""
        manifest = self._plugin
        if manifest is None or not self._watched or self._working:
            return
        if not any(item["id"] == photo_id for item in self._state["items"]):
            return
        plugin_id = manifest.id
        generation = self._generation

        def fetch() -> object:
            if not self._current(generation):
                return None
            return photos_view.fetch_current_original(plugin_id, str(photo_id))

        def completed(value: object) -> None:
            if value is None or not self._current(generation):
                return
            self._state = photos_view.with_original(self._state, photo_id, self._url(value))
            self._on_change()
            self._open_file(value)

        def failed(message: str) -> None:
            if self._current(generation):
                self._on_error(message)

        self._run(self._open_runner, fetch, completed, failed)
