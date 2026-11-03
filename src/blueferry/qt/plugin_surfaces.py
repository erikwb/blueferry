"""Qt adapter for the plugin surfaces (PLUGINS.md, ApiVersion 1.2).

The logic lives in :class:`blueferry.plugin_surfaces_model.PluginSurfacesModel`
(shared with the GTK client); this adapter runs it on a private
``QThreadPool`` and a ``QTimer``, turns file URLs from QML into paths and
watches ``Plugin1.CardChanged`` with QtDBus only from the owners of the
enabled card plugins' bus names the model hands it (QtDBus follows the owner
of each watched name), so no other session-bus client can make the card
call every plugin in a loop. QML sees one plain ``state`` map.

It also hosts the main window's drop zone,
:class:`blueferry.file_drop_view.FileDrop`, on the same pool: files dropped
anywhere on the window go to a "Send to…" target.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import SLOT, QObject, QThreadPool, QUrl, Signal, Slot

from blueferry import plugin_surfaces as surfaces
from blueferry.file_drop_view import FileDrop
from blueferry.plugin_api import OBJECT_PATH, SIGNAL_CARD_CHANGED, SURFACES_INTERFACE
from blueferry.plugin_surfaces_model import PluginSurfacesModel, SendGate
from blueferry.qt.tasks import QtTaskRunner, qt_timers


def open_url(uri: str) -> None:
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(uri))


class PluginSurfaces(QObject):
    changed = Signal()
    # The drop zone's picker or explanation changed.
    dropChanged = Signal()
    # A short notice about a drop (text, ok): a passive notification.
    dropNotice = Signal(str, bool)

    def __init__(
        self,
        *,
        load_cards: Callable[[], list[surfaces.PluginCard]] = surfaces.load_cards,
        load_targets: Callable[[], surfaces.ShareTargets] = surfaces.load_targets,
        card_names: Callable[[], list[str]] = surfaces.card_bus_names,
        share_available: Callable[[], bool] = surfaces.share_available,
        invoke: Callable[..., surfaces.Outcome] = surfaces.invoke,
        find: Callable[[str, str], Any] = surfaces.find_plugin,
        send: Callable[..., surfaces.Outcome] = surfaces.send,
        card_choice: Callable[..., surfaces.ShareChoice | None] = surfaces.card_choice,
        opener: Callable[[str], None] = open_url,
        bus: Any = None,
        pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self._watched: set[str] = set()
        if pool is None:
            pool = QThreadPool(self)
            pool.setMaxThreadCount(2)
        self._pool = pool
        # One SendFiles at a time across "Send to…", card sends and drops.
        gate = SendGate()
        self.model = PluginSurfacesModel(
            runner=QtTaskRunner(pool), timer=qt_timers(self), opener=opener,
            on_change=self.changed.emit, watch=self._watch,
            load_cards=load_cards, load_targets=load_targets, card_names=card_names,
            share_available=share_available, invoke=invoke, find=find, send=send,
            card_choice=card_choice, gate=gate,
        )
        self.file_drop = FileDrop(
            runner=QtTaskRunner(pool), notify=self.dropNotice.emit,
            on_change=self.dropChanged.emit, on_sent=self.model.schedule_reload,
            load_targets=load_targets, send=send, gate=gate,
        )

    def state(self) -> dict:
        return self.model.state()

    # ---- card ---------------------------------------------------------------------

    @Slot()
    def _cardChanged(self) -> None:
        self.model.schedule_reload()

    def schedule_reload(self) -> None:
        """One coalesced reload, e.g. after a plugin was enabled."""
        self.model.schedule_reload()

    def reload(self) -> None:
        self.model.reload()

    def _watch(self, names: Sequence[str]) -> None:
        """Hear CardChanged only from the owners of these bus names."""
        if self._bus is None:
            return
        wanted = {str(name) for name in names if name}
        for name in sorted(self._watched - wanted):
            self._bus.disconnect(name, OBJECT_PATH, SURFACES_INTERFACE, SIGNAL_CARD_CHANGED,
                                 self, SLOT("_cardChanged()"))
        for name in sorted(wanted - self._watched):
            self._bus.connect(name, OBJECT_PATH, SURFACES_INTERFACE, SIGNAL_CARD_CHANGED,
                              self, SLOT("_cardChanged()"))
        self._watched = wanted

    def invoke(self, plugin_id: str, item_id: str, action_id: str) -> None:
        self.model.invoke(plugin_id, item_id, action_id)

    def clear_message(self) -> None:
        self.model.clear_message()

    # ---- share ------------------------------------------------------------------

    def load_targets(self) -> None:
        self.model.load_targets()

    def send(self, key: str, urls: Sequence[object]) -> None:
        # FileDialog.selectedFiles hands over QUrl objects; plain strings are
        # either file: URLs or paths.
        self.model.send(key, [local_path(url) for url in urls])

    def send_from_card(
        self, plugin_id: str, target_id: str, label: str, urls: Sequence[object],
    ) -> None:
        """A card action with ``send_to``, or files dropped on its item."""
        self.model.send_from_card(plugin_id, target_id, label,
                                  [local_path(url) for url in urls])

    # ---- drop zone ----------------------------------------------------------------

    def drop(self, urls: Sequence[object]) -> None:
        """Files dropped on the main window (QML ``drop.urls``)."""
        self.file_drop.drop([uri_text(url) for url in urls])


def uri_text(url: object) -> str:
    """A dropped URL as encoded text, so ``%`` in a file name survives."""
    if isinstance(url, QUrl):
        return bytes(url.toEncoded().data()).decode("ascii", "replace")
    return str(url)


def local_path(url: object) -> str:
    if isinstance(url, QUrl):
        return url.toLocalFile() if url.isLocalFile() else url.toString()
    text = str(url)
    return QUrl(text).toLocalFile() if text.startswith("file:") else text
