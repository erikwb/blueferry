"""Qt adapter for the plugin surfaces (PLUGINS.md, ApiVersion 1.2).

The phone card's "From Plugins" section (capability ``card``) and "Send to…"
(capability ``share``) call plugins over D-Bus with timeouts, so every call
runs on a private pool; the D-Bus worker of the controller and the UI thread
never wait on a plugin. A content-free ``Plugin1.CardChanged`` schedules one
coalesced reload; it is only heard from the owner of an enabled card
plugin's bus name (QtDBus follows the owner of each watched name), so no
other session-bus client can make the card call every plugin in a loop.
QML sees one plain ``state`` map.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import SLOT, QObject, QThreadPool, QTimer, QUrl, Signal, Slot

from blueferry import plugin_surfaces as surfaces
from blueferry.i18n import _
from blueferry.plugin_api import OBJECT_PATH, SIGNAL_CARD_CHANGED, SURFACES_INTERFACE
from blueferry.plugin_api.surfaces import checked_share_paths
from blueferry.qt.tasks import Task
from blueferry.text_safety import control_text

RELOAD_DELAY_MS = 400


def open_url(uri: str) -> None:
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(uri))


class PluginSurfaces(QObject):
    changed = Signal()

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
        self._load_cards = load_cards
        self._load_targets = load_targets
        self._card_names = card_names
        self._bus = bus
        self._watched: set[str] = set()
        self._share_available = share_available
        self._invoke = invoke
        self._find = find
        self._send = send
        self._card_choice = card_choice
        self._open = opener
        if pool is None:
            pool = QThreadPool(self)
            pool.setMaxThreadCount(2)
        self._pool = pool
        self._tasks: set[Task] = set()
        self._choices: dict[str, surfaces.ShareChoice] = {}
        self._loading = False
        self._reload_again = False
        self._state: dict[str, Any] = {
            "loaded": False, "cards": [], "busy": "", "message": "", "messageOk": True,
            "targets": [], "targetsLoaded": False, "targetProblems": [],
            "targetsLoading": False, "shareAvailable": False,
        }
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RELOAD_DELAY_MS)
        self._timer.timeout.connect(self.reload)

    def state(self) -> dict:
        return dict(self._state)

    def _update(self, **values: Any) -> None:
        self._state = {**self._state, **values}
        self.changed.emit()

    def _run(self, work: Callable[[], object], done: Callable[[Any], None],
             failed: Callable[[str], None] | None = None) -> None:
        task = Task(work)
        self._tasks.add(task)
        task.signals.done.connect(done)
        task.signals.failed.connect(failed or (lambda message: self._say(
            message or _("The plugin did not answer."), False)))
        task.signals.finished.connect(lambda: self._tasks.discard(task))
        self._pool.start(task)

    def _say(self, message: str, ok: bool) -> None:
        self._update(message=message, messageOk=ok, busy="")

    # ---- card ---------------------------------------------------------------------

    @Slot()
    def _cardChanged(self) -> None:
        self._timer.start()

    def schedule_reload(self) -> None:
        """One coalesced reload, e.g. after a plugin was enabled."""
        self._timer.start()

    def reload(self) -> None:
        """Fetch every card plugin's items again (coalesced)."""
        if self._loading:
            self._reload_again = True
            return
        self._loading = True

        def work() -> tuple:
            return self._load_cards(), self._share_available(), self._card_names()

        def done(value: tuple) -> None:
            cards, share, names = value
            self._loading = False
            self._watch(names)
            self._update(loaded=True, cards=surfaces.card_rows(cards), shareAvailable=bool(share))
            if self._reload_again:
                self._reload_again = False
                self._timer.start()

        def failed(message: str) -> None:
            self._loading = False
            self._update(loaded=True)
            self._say(message or _("Plugins are unavailable."), False)

        self._run(work, done, failed)

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
        if self._state["busy"]:
            return
        self._update(busy=f"{plugin_id}:{item_id}:{action_id}", message="")

        def work() -> surfaces.Outcome:
            return self._invoke(self._find(plugin_id, "card"), item_id, action_id)

        def done(outcome: surfaces.Outcome) -> None:
            self._say(outcome.message, outcome.ok)
            if outcome.ok and outcome.open_uri:
                self._open(outcome.open_uri)
            self._timer.start()

        self._run(work, done)

    def clear_message(self) -> None:
        self._update(message="")

    # ---- share ------------------------------------------------------------------

    def load_targets(self) -> None:
        """Ask the share plugins for targets (starts them); only when the
        user opens "Send to…"."""
        if self._state["targetsLoading"]:
            return
        self._update(targetsLoading=True)

        def failed(message: str) -> None:
            self._update(targetsLoading=False, targetsLoaded=True)
            self._say(message or _("The plugins did not answer."), False)

        def done(targets: surfaces.ShareTargets) -> None:
            self._choices = {choice.key: choice for choice in targets.choices}
            self._update(
                targetsLoading=False,
                shareAvailable=bool(targets.choices) or self._state["shareAvailable"],
                targetsLoaded=True,
                targets=[
                    {"key": choice.key, "icon": choice.icon or "document-send",
                     "label": surfaces.choice_label(choice, targets.choices),
                     # Menu items detect rich text; the label is for plain text.
                     "menuText": control_text(surfaces.choice_label(choice, targets.choices))}
                    for choice in targets.choices
                ],
                targetProblems=list(targets.problems),
            )

        self._run(self._load_targets, done, failed)

    def send(self, key: str, urls: Sequence[object]) -> None:
        choice = self._choices.get(key)
        if choice is None:
            self._say(_("That target is gone; open \u201cSend to\u201d again."), False)
            return
        # FileDialog.selectedFiles hands over QUrl objects; plain strings are
        # either file: URLs or paths.
        paths = [local_path(url) for url in urls]
        self._update(busy=f"send:{key}", message="")
        self._send_paths(lambda: choice, paths)

    def send_from_card(
        self, plugin_id: str, target_id: str, label: str, urls: Sequence[object],
    ) -> None:
        """A card action with ``send_to`` (or files dropped on its item):
        hand the files to that target of the same plugin (ApiVersion 1.4)."""
        if self._state["busy"]:
            return
        paths = [local_path(url) for url in urls]
        if not paths:
            return
        self._update(busy=f"send:{plugin_id}:{target_id}", message="")
        self._send_paths(lambda: self._card_choice(plugin_id, target_id, label), paths)

    def _send_paths(
        self, resolve: Callable[[], surfaces.ShareChoice | None], paths: list[str],
    ) -> None:
        def work() -> surfaces.Outcome:
            try:
                choice = resolve()  # manifests and the plugin: not on the UI thread
            except LookupError as error:
                return surfaces.Outcome(False, str(error))
            if choice is None:
                return surfaces.Outcome(False, _("The plugin can no longer send files."))
            try:
                files = checked_share_paths(paths)  # stat()s: not on the UI thread
            except ValueError as error:
                return surfaces.Outcome(False, str(error))
            return self._send(choice, files)

        def done(outcome: surfaces.Outcome) -> None:
            self._say(outcome.message, outcome.ok)
            self._timer.start()

        self._run(work, done)


def local_path(url: object) -> str:
    if isinstance(url, QUrl):
        return url.toLocalFile() if url.isLocalFile() else url.toString()
    text = str(url)
    return QUrl(text).toLocalFile() if text.startswith("file:") else text
