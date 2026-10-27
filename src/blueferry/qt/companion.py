"""Qt adapter for the companion tools (the phone card's "Tools").

The logic lives in :class:`blueferry.companion_model.CompanionToolsModel`
(shared with the GTK client); this adapter runs it on a private
single-thread ``QThreadPool`` and turns its changes and reports into Qt
signals.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThreadPool, Signal

from blueferry.companion_model import CompanionToolsModel
from blueferry.companion_tools import Snapshot, System
from blueferry.qt.tasks import QtTaskRunner


class CompanionTools(QObject):
    """State of the companion tools plus the actions behind their buttons."""

    changed = Signal()
    # ok, plain-text message of the last action (for a client that shows it).
    reported = Signal(bool, str)

    def __init__(self, system: System | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self.model = CompanionToolsModel(
            runner=QtTaskRunner(self._pool), system=system,
            on_change=self.changed.emit, on_report=self.reported.emit,
        )

    @property
    def snapshot(self) -> Snapshot:
        return self.model.snapshot

    @property
    def busy(self) -> str:
        return self.model.busy

    @property
    def needs_pairing(self) -> bool:
        return self.model.needs_pairing

    def state(self) -> dict:
        """QML-friendly copy: tool rows, the running action and the last message."""
        return self.model.state()

    def refresh(self) -> None:
        """Re-read which tools exist and whether an iPhone is on USB."""
        self.model.refresh()

    def run(self, action: str) -> None:
        self.model.run(action)

    def clear_message(self) -> None:
        self.model.clear_message()
