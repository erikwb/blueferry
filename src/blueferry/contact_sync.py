"""Coordinate phonebook (PBAP) downloads into the contact cache.

Automatic pulls give MAP a bounded head start after profiles come up, so a
bulk transfer on the shared OBEX worker does not delay message access. Manual
requests join a pull already in flight. Everything here runs on the GLib loop
except the download itself, which runs on the OBEX worker.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import GLib

from blueferry import contacts as contacts_module
from blueferry.contacts import ContactsResolver, clear_contact_cache
from blueferry.limits import MAX_OBEX_PENDING_OPERATIONS
from blueferry.storage_security import StorageSecurity

if TYPE_CHECKING:
    from blueferry.obex.sessions import SessionManager

log = logging.getLogger(__name__)

# How often to re-pull the iPhone's phonebook (so the cache picks up new contacts)
CONTACTS_REFRESH_SEC = 24 * 60 * 60  # 24h
# Give initial MAP retries the permission window before starting a bulk PBAP
# transfer, but keep contact sync available when only PBAP ever connects.
CONTACTS_MAP_GRACE_SECONDS = 180

Success = Callable[[int], None]
Failure = Callable[[Exception], None]
Pull = Callable[..., int]


class StorageChangedDuringSync(RuntimeError):
    """Local storage changed policy or key while a pull was writing the cache."""


class ContactSync:
    def __init__(
        self,
        *,
        sessions: SessionManager,
        storage: StorageSecurity,
        contacts: ContactsResolver,
        submit: Callable[..., object],
        on_refreshed: Callable[[], None],
        pull: Pull | None = None,
        schedule: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], object] | None = None,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._contacts = contacts
        self._submit = submit
        self._on_refreshed = on_refreshed
        self._pull = pull
        # Resolve GLib lazily so a test's patched main loop is honoured.
        self._schedule = schedule or (
            lambda delay, callback: GLib.timeout_add_seconds(delay, callback)
        )
        self._cancel = cancel or (lambda source: GLib.source_remove(source))
        self._pending = False
        self._initial_sync_done = False
        self._generation = 0
        self._waiters: list[tuple[Success, Failure]] = []
        self._deferred = False
        self._map_wait_id: int | None = None
        self._map_wait_finished = False
        self._periodic_id: int | None = None

    @property
    def pending(self) -> bool:
        return self._pending

    @property
    def deferred(self) -> bool:
        """An automatic pull is owed once MAP connects or its grace expires."""
        return self._deferred

    @property
    def initial_sync_done(self) -> bool:
        return self._initial_sync_done

    @property
    def waiting(self) -> int:
        """Manual callers joined to the pull in flight."""
        return len(self._waiters)

    def needed(self) -> bool:
        """Whether startup or recovery still owes an automatic contact sync."""
        return self._deferred or (
            not self._initial_sync_done and self._contacts.count() == 0
        )

    def profiles_available(self) -> None:
        """Start owed pulls and the daily refresh once PBAP is live."""
        if self._sessions.pbap is None:
            return
        # Bulk PBAP transfers share the MAP worker. Defer automatic pulls
        # during MAP's initial grace period so message access can retry promptly.
        if self.needed():
            self.refresh()
        if self._periodic_id is None:
            self._periodic_id = self._schedule(CONTACTS_REFRESH_SEC, self._periodic)

    def storage_changed(self) -> None:
        if self._storage.status.can_write and self.needed():
            self.refresh()

    def storage_prepared(self) -> None:
        # Preparing storage can replace an archive cleared by a policy change.
        # An older in-flight pull must not satisfy this cache's initial sync.
        self._generation += 1
        self._initial_sync_done = False

    def refresh(self) -> None:
        """Best-effort automatic pull used by startup, recovery, and the timer."""
        if (
            self._pending
            or self._sessions.pbap is None
            or not self._storage.status.can_write
        ):
            return
        if self._sessions.map is None and not self._map_wait_finished:
            self._deferred = True
            if self._map_wait_id is None:
                self._map_wait_id = self._schedule(
                    CONTACTS_MAP_GRACE_SECONDS, self._map_wait_expired,
                )
            return
        self._finish_map_wait()
        self.sync()

    def sync(self, success: Success | None = None, failure: Failure | None = None) -> None:
        """Join or start one contact pull; called and completed on GLib."""
        if success is not None and failure is not None:
            # Coalescing must retain the worker queue's bound on callers.
            if len(self._waiters) >= MAX_OBEX_PENDING_OPERATIONS:
                failure(RuntimeError("too many pending contact sync requests"))
                return
            self._waiters.append((success, failure))
        if self._pending:
            return
        self._pending = True
        generation = self._generation
        # The download can take minutes. Give it a private key buffer: the
        # live one is zeroed in place whenever storage relocks, changes
        # policy, or fails closed.
        revision = self._storage.revision
        storage = self._storage.snapshot()
        pull = self._pull or contacts_module.pull_phonebook

        def download() -> int:
            try:
                return pull(self._sessions, storage=storage)
            finally:
                storage.close()

        def succeeded(pulled: int) -> None:
            try:
                count = self._pulled(pulled, revision)
            except Exception as error:
                failed(error)
            else:
                self._finished(generation, count=count)

        def failed(error: Exception) -> None:
            self._finished(generation, error=error)

        try:
            self._submit(download, on_success=succeeded, on_error=failed)
        except Exception as error:
            storage.close()
            failed(error)

    def stop(self) -> None:
        for attribute in ("_periodic_id", "_map_wait_id"):
            source = getattr(self, attribute)
            if source is not None:
                try:
                    self._cancel(source)
                except Exception:
                    log.debug("could not remove contact sync timer", exc_info=True)
                setattr(self, attribute, None)

    def _pulled(self, pulled: int, revision: int) -> int:
        """GLib-side cache refresh after a successful PBAP pull."""
        if self._storage.revision != revision:
            # The worker sealed the cache under a policy or key that is no
            # longer current. It is only a cache of the phone's contacts, so
            # erase it and download again rather than keep unreadable or
            # under-protected rows.
            clear_contact_cache()
            self._contacts.refresh()
            self._initial_sync_done = False
            raise StorageChangedDuringSync(
                "local storage changed during contact sync; downloading again"
            )
        # Manual sync also satisfies a deferred automatic refresh.
        self._finish_map_wait()
        count = self._contacts.refresh()
        self._on_refreshed()
        log.info("contacts refresh: pulled %d, cached %d", pulled, count)
        return count

    def _finished(
        self, generation: int, *, count: int = 0, error: Exception | None = None,
    ) -> None:
        waiters, self._waiters = self._waiters, []
        self._pending = False
        stale = isinstance(error, StorageChangedDuringSync)
        if error is None and generation == self._generation:
            # Zero usable destinations is still a successful sync. Retrying
            # MAP must not repeatedly download the same empty contact cache.
            self._initial_sync_done = True
        if stale:
            log.info("discarded a contact pull: %s", error)
        elif error is not None:
            log.error("contacts refresh failed; using previous cache: %s", error)
            try:
                self._sessions.report_error(error)
            except Exception:
                log.exception("could not report contact sync transport failure")
        for success, failure in waiters:
            try:
                if error is None:
                    success(count)
                else:
                    failure(error)
            except Exception:
                log.exception("contact sync completion callback failed")
        if (
            error is not None
            and self._deferred
            and self._map_wait_finished
        ):
            # A manual pull can span the grace deadline. Its failure must not
            # consume the deferred automatic request. Automatic attempts clear
            # this flag before queuing, so this cannot form a retry loop.
            self.refresh()
        elif (stale or generation != self._generation) and self.needed():
            # Storage changed while this pull was pending, so its recovery
            # callback could not queue the replacement download yet.
            self.refresh()

    def _finish_map_wait(self) -> None:
        self._map_wait_finished = True
        self._deferred = False
        if self._map_wait_id is not None:
            self._cancel(self._map_wait_id)
            self._map_wait_id = None

    def _map_wait_expired(self) -> bool:
        self._map_wait_id = None
        self._map_wait_finished = True
        # If PBAP or storage is unavailable at expiry, leave the deferred
        # flag set so their recovery triggers the download without a new wait.
        self.refresh()
        return False

    def _periodic(self) -> bool:
        """GLib timeout callback. Return True to keep the timer running."""
        log.info("periodic contacts refresh tick")
        self.refresh()
        return True
