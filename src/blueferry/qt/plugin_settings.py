"""Qt adapter for plugin management (settings category "Plugins").

The logic lives in :class:`blueferry.plugin_settings_model.PluginSettingsModel`
(shared with the GTK client); this adapter runs it on a private
single-thread ``QThreadPool`` and a ``QTimer``, so the D-Bus worker of the
controller is never held up, and turns its changes into the ``changed``
signal. QML sees the plain ``state`` map and calls the slots the controller
forwards.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, QUrl, Signal

from blueferry.plugin_api.client import PluginClient
from blueferry.plugin_api.config_flow import LOGIN_POLL_SECONDS
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.plugin_index import PluginIndex
from blueferry.plugin_manager import PluginManager
from blueferry.plugin_settings_model import PluginSettingsModel
from blueferry.qt.tasks import QtTaskRunner, qt_timers


def open_url(uri: str) -> None:
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(uri))


class PluginSettings(QObject):
    changed = Signal()

    def __init__(
        self,
        *,
        manager: Callable[[], PluginManager] = PluginManager,
        index: Callable[[], PluginIndex] = PluginIndex,
        client: Callable[[PluginManifest], PluginClient] = PluginClient,
        pool: QThreadPool | None = None,
        opener: Callable[[str], None] = open_url,
        poll_ms: int = LOGIN_POLL_SECONDS * 1000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pool = pool or QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self.model = PluginSettingsModel(
            runner=QtTaskRunner(self._pool), timer=qt_timers(self), opener=opener,
            on_change=self.changed.emit, manager=manager, index=index, client=client,
            poll_ms=poll_ms,
        )

    def state(self) -> dict:
        return self.model.state()

    def load(self) -> None:
        self.model.load()

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        self.model.set_enabled(plugin_id, enabled)

    def prepare_install(self, url: str, ref: str = "") -> None:
        self.model.prepare_install(url, ref)

    def install_from_store(self, plugin_id: str) -> None:
        self.model.install_from_store(plugin_id)

    def prepare_update(self, plugin_id: str) -> None:
        self.model.prepare_update(plugin_id)

    def confirm_install(self) -> None:
        self.model.confirm_install()

    def cancel_install(self) -> None:
        self.model.cancel_install()

    def remove(self, plugin_id: str) -> None:
        self.model.remove(plugin_id)

    def load_config(self, plugin_id: str) -> None:
        self.model.load_config(plugin_id)

    def check_config(self, plugin_id: str, raw: Mapping[str, object]) -> dict[str, Any]:
        return self.model.check_config(plugin_id, raw)

    def save_config(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        self.model.save_config(plugin_id, raw)

    def test_config(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        self.model.test_config(plugin_id, raw)

    def sign_in(self, plugin_id: str, raw: Mapping[str, object]) -> None:
        self.model.sign_in(plugin_id, raw)

    def cancel_sign_in(self) -> None:
        self.model.cancel_sign_in()

    def run_action(self, plugin_id: str, action_id: str) -> None:
        self.model.run_action(plugin_id, action_id)

    def open_help(self, plugin_id: str, key: str) -> None:
        self.model.open_help(plugin_id, key)

    def open_log(self, plugin_id: str) -> None:
        self.model.open_log(plugin_id)

    def close_config(self) -> None:
        self.model.close_config()

    def load_store(self, refresh: bool) -> None:
        self.model.load_store(refresh)

    def set_indexes(self, urls: list[str]) -> None:
        self.model.set_indexes(urls)

    def clear_message(self) -> None:
        self.model.clear_message()
