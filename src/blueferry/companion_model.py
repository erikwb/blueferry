"""Toolkit-neutral state of :mod:`blueferry.companion_tools` (the phone
card's "Tools"), shared by Qt (``qt/companion.py``) and, soon, GTK.

Probing (``idevice_id -l``) and the photo actions run external programs, so
they run on the client's :class:`~blueferry.client_tasks.TaskRunner` (one
worker thread); a slow USB device never holds up the UI or the client's
D-Bus worker. Starting UxPlay or LocalSend (pid files, a Gio spawn) runs
there as well, so the UI thread never touches the disk.
"""
from __future__ import annotations

from collections.abc import Callable

from blueferry import companion_tools
from blueferry.client_tasks import TaskRunner
from blueferry.companion_tools import ActionResult, Snapshot, System
from blueferry.i18n import _


class CompanionToolsModel:
    """State of the companion tools plus the actions behind their buttons.

    ``on_report(ok, message)`` hears the plain-text result of every action
    (for a client that shows it as a notification).
    """

    def __init__(
        self,
        *,
        runner: TaskRunner,
        system: System | None = None,
        on_change: Callable[[], None] = lambda: None,
        on_report: Callable[[bool, str], None] = lambda _ok, _message: None,
    ) -> None:
        self._system = system or companion_tools.default_system()
        self._runner = runner
        self._on_change = on_change
        self._on_report = on_report
        # Empty until the first refresh: even the cheap lookups stay off the UI thread.
        self._snapshot = Snapshot()
        self._probed = False
        self._refreshing = False
        self._busy = ""
        self._message = ""
        self._message_ok = True
        self._needs_pairing = False

    # ---- state ------------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def busy(self) -> str:
        return self._busy

    @property
    def needs_pairing(self) -> bool:
        return self._needs_pairing

    def state(self) -> dict:
        """Plain copy for the clients: tool rows, the running action and the last message."""
        return {
            "probed": self._probed,
            "tools": [
                {
                    "key": tool.key, "installed": tool.installed, "enabled": tool.enabled,
                    "active": tool.active, "title": tool.title, "subtitle": tool.subtitle,
                }
                for tool in self._snapshot.tools
            ],
            "busy": self._busy,
            "message": self._message,
            "messageOk": self._message_ok,
            "needsPairing": self._needs_pairing,
        }

    # ---- work ---------------------------------------------------------------

    def _submit(
        self, operation: Callable[[], object], done: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        self._runner.submit(operation, done, failed)

    def refresh(self) -> None:
        """Re-read which tools exist and whether an iPhone is on USB."""
        if self._refreshing:
            return
        self._refreshing = True
        system = self._system

        def done(value: object) -> None:
            self._refreshing = False
            if isinstance(value, Snapshot):
                self._snapshot = value
                self._probed = True
                if self._needs_pairing and not self._photos_enabled():
                    self._needs_pairing = False
                self._on_change()

        def failed(_message: str) -> None:
            self._refreshing = False

        self._submit(lambda: companion_tools.snapshot(system), done, failed)

    def _photos_enabled(self) -> bool:
        photos = self._snapshot.get(companion_tools.PHOTOS)
        return photos is not None and photos.installed and (photos.enabled or photos.active)

    def run(self, action: str) -> None:
        if action not in companion_tools.ACTIONS or self._busy:
            return
        # Starting UxPlay or LocalSend reads and writes pid files and spawns a
        # process; the photo actions run ifuse: all of it off the UI thread.
        # Check plugin replacements on the worker at execution time as well:
        # the snapshot can be stale, or a visible Stop row may have exited.
        self._busy = action
        self._on_change()
        system = self._system

        def work() -> ActionResult:
            return companion_tools.perform(system, action)

        def done(value: object) -> None:
            self._busy = ""
            if isinstance(value, ActionResult):
                self._report(value, action)
            self.refresh()

        def failed(_message: str) -> None:
            self._busy = ""
            self._report(ActionResult(False, _("The action failed unexpectedly.")), action)
            self.refresh()

        self._submit(work, done, failed)

    def _report(self, result: ActionResult, action: str) -> None:
        self._message = result.message
        self._message_ok = result.ok
        if result.needs_pairing:
            self._needs_pairing = True
        elif action in (companion_tools.PHOTOS, companion_tools.PAIR) and result.ok:
            self._needs_pairing = False
        self._on_change()
        self._on_report(result.ok, result.message)

    def clear_message(self) -> None:
        if self._message:
            self._message = ""
            self._on_change()
