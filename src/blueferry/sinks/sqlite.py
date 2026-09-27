"""Persistent event sink backed by the private SQLite history store."""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.events import SmsEvent
from blueferry.history import append_event, minimize_ancs_history, prune_events

if TYPE_CHECKING:
    from blueferry.storage_security import StorageSecurity

log = logging.getLogger(__name__)

# Count and size ceilings are cheap and enforced every few writes. The age
# sweep decrypts the whole archive, so it runs at most this often; storage
# preparation also runs it at every startup and unlock.
AGE_SWEEP_INTERVAL_SEC = 60 * 60


class SqliteSink:
    name = "sqlite"

    def __init__(
        self,
        path: Path | None = None,
        *,
        storage: StorageSecurity | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.storage = storage
        self._clock = clock
        self._last_age_sweep = clock()
        self._writes_since_prune = 0
        self._unavailable_logged = False
        if storage is not None:
            # Managed stores are prepared by the asynchronous storage lifecycle.
            # Sink creation must not repeat those archive scans on the GLib loop.
            log.info("SQLite history sink: %s", storage.status.detail)
            return
        discarded, minimized = minimize_ancs_history(
            path=self.path, storage=self.storage
        )
        if discarded or minimized:
            log.info(
                "minimized ANCS history (discarded=%d, compacted=%d)",
                discarded,
                minimized,
            )
        removed = prune_events(path=self.path, storage=self.storage)
        if removed:
            log.info("pruned %d expired history events", removed)
        log.info("SQLite history sink ready")

    def handle(self, event: SmsEvent) -> None:
        self._append(event.to_dict())

    def handle_ancs(self, event) -> None:
        if event.app_id != MESSAGES_APP_ID:
            return
        self._append(event.correlation_dict())

    def _append(self, payload: dict) -> None:
        if self.storage is not None and not self.storage.status.can_write:
            if not self._unavailable_logged:
                log.info("not retaining history: %s", self.storage.status.detail)
                self._unavailable_logged = True
            return
        try:
            append_event(payload, path=self.path, storage=self.storage)
            self._writes_since_prune += 1
            if self._writes_since_prune >= 10:
                self._writes_since_prune = 0
                now = self._clock()
                sweep = now - self._last_age_sweep >= AGE_SWEEP_INTERVAL_SEC
                if sweep:
                    self._last_age_sweep = now
                removed = prune_events(
                    path=self.path, storage=self.storage, expire_by_age=sweep,
                )
                if removed:
                    log.info("pruned %d expired history events", removed)
        except (OSError, TypeError, ValueError, sqlite3.Error):
            log.exception("SQLite history write failed")
