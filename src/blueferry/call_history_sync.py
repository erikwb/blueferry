"""Schedule PBAP call-history pulls and announce newly missed calls.

The blocking part of a sync, three small PBAP transfers plus the encrypted
replacement write, runs on the shared OBEX worker. Everything else, including
the in-memory snapshot served to D-Bus clients and the missed-call callback,
runs on the GLib loop. PBAP has no change notification, so the phone is polled
at ``BLUEFERRY_CALL_HISTORY_INTERVAL_SEC`` and on explicit client request.

Automatic pulls yield to MAP, which shares the single worker (see #165 and
``ContactSync``): while a MAP session that was connected is being
re-established, every automatic pull is deferred until profiles report
availability again. When MAP has never connected in this daemon, automatic
pulls wait for the same three-minute grace period as contact sync and the
periodic tick stays off; only prompt, grace-expiry, and requested pulls run.
Explicit client syncs are never gated.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from gi.repository import GLib

from blueferry import config
from blueferry import contacts as contacts_module
from blueferry.call_history import (
    PHONEBOOKS,
    CallRecord,
    merge_call_history,
    parse_call_history,
)
from blueferry.call_history_repository import (
    CallHistoryRepository,
    ReplaceResult,
    clear_call_history,
)
from blueferry.contact_sync import CONTACTS_MAP_GRACE_SECONDS
from blueferry.limits import (
    MAX_CALL_HISTORY_BYTES,
    MAX_CALL_HISTORY_PER_FOLDER,
    MAX_OBEX_PENDING_OPERATIONS,
)
from blueferry.storage_security import CorruptStorageError

if TYPE_CHECKING:
    from blueferry.obex.sessions import SessionManager
    from blueferry.storage_security import StorageSecurity

log = logging.getLogger(__name__)

# Let MAP's first message fetch and the contact pull reach the worker first.
CALL_HISTORY_INITIAL_DELAY_SEC = 20
# Same head start as contact sync gives MAP's initial permission retries.
CALL_HISTORY_MAP_GRACE_SECONDS = CONTACTS_MAP_GRACE_SECONDS
# Coalescing window for request_sync(): iOS writes its call log shortly after
# a call ends, and bursts of requests should produce a single pull.
CALL_HISTORY_REQUEST_DELAY_SEC = 5
# Upper bound for one call-history listing transfer. These listings are small;
# a stuck transfer must not hold the shared worker for the phonebook's 30 min.
CALL_HISTORY_TRANSFER_MAX_SECONDS = 120
# Never announce a call that is older than this, even if it was never seen:
# after a long time offline the list is useful, a burst of stale popups is not.
MISSED_CALL_NOTIFY_MAX_AGE = timedelta(hours=12)

Success = Callable[[int], None]
Failure = Callable[[Exception], None]
Pull = Callable[..., list[CallRecord]]


class StorageChangedDuringCallSync(RuntimeError):
    """Local storage changed policy or key while a sync was writing."""


class CallHistoryStorageError(RuntimeError):
    """The pull worked but the local mirror could not be written."""


def pull_call_history(sessions: SessionManager) -> list[CallRecord]:
    """Pull ich/och/mch individually and merge them. Worker thread only.

    The combined ``cch`` phonebook is deliberately not used: it is reported
    to be incomplete on iOS, and the directional lists carry the same calls.
    """
    lists: list[list[CallRecord]] = []
    for phonebook, direction in PHONEBOOKS:
        blob = contacts_module.pull_vcard_listing(
            sessions,
            phonebook,
            max_entries=MAX_CALL_HISTORY_PER_FOLDER,
            max_bytes=MAX_CALL_HISTORY_BYTES,
            # An empty missed-calls list is an ordinary answer.
            allow_empty=True,
            overall_timeout_s=CALL_HISTORY_TRANSFER_MAX_SECONDS,
        )
        parsed = parse_call_history(blob, folder_direction=direction)
        log.info("PBAP %s: %d calls", phonebook, len(parsed))
        lists.append(parsed)
    return merge_call_history(lists)


class CallHistorySync:
    """Own the call-history snapshot, its timer, and the joined manual sync."""

    def __init__(
        self,
        *,
        sessions: SessionManager,
        storage: StorageSecurity,
        submit: Callable[..., object],
        on_changed: Callable[[], None],
        on_missed: Callable[[list[CallRecord]], None],
        pull: Pull | None = None,
        schedule: Callable[[int, Callable[[], bool]], int] | None = None,
        cancel: Callable[[int], object] | None = None,
        interval: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._submit = submit
        self._on_changed = on_changed
        self._on_missed = on_missed
        self._pull = pull
        self._schedule = schedule or (
            lambda delay, callback: GLib.timeout_add_seconds(delay, callback)
        )
        self._cancel = cancel or (lambda source: GLib.source_remove(source))
        self._interval = interval or config.CALL_HISTORY_INTERVAL_SEC
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: list[CallRecord] = []
        self._pending = False
        self._waiters: list[tuple[Success, Failure]] = []
        self._periodic_id: int | None = None
        self._initial_id: int | None = None
        self._stopped = False
        self._synced = False
        # MAP gating (mirrors ContactSync; see module docstring).
        self._deferred = False
        self._map_seen = False
        self._map_wait_id: int | None = None
        self._map_wait_finished = False
        # request_sync(): one coalescing timer, and one follow-up pull when a
        # request arrives while a pull (possibly started too early) runs.
        self._request_id: int | None = None
        self._resync = False

    @property
    def pending(self) -> bool:
        return self._pending

    @property
    def deferred(self) -> bool:
        """An automatic pull is owed once MAP is back or its grace expires."""
        return self._deferred

    def records(self) -> list[CallRecord]:
        """Newest-first retained calls; expired entries are filtered out."""
        cutoff = self._clock() - timedelta(days=config.HISTORY_RETENTION_DAYS)
        return [record for record in self._records if record.occurred_at >= cutoff]

    def adopt(self, records: list[CallRecord]) -> None:
        """Install records loaded by worker-side storage preparation."""
        changed = records != self._records
        self._records = list(records)
        if changed:
            self._on_changed()

    def discard_cache(self) -> None:
        """Drop the in-memory snapshot after history or policy was cleared."""
        if self._records:
            self._records = []
            self._on_changed()

    def storage_changed(self) -> None:
        if not self._storage.status.can_read:
            self.discard_cache()
        elif not self._synced or self._deferred:
            # A wallet unlocked after PBAP connected: do not wait a full
            # polling interval for the first list. MAP gating still applies.
            self.refresh("storage")

    def profiles_available(self) -> None:
        """Start the polling timer and one prompt sync once PBAP is live.

        Also called when MAP returns, which is where a deferred pull is
        caught up.
        """
        if self._stopped or self._sessions.pbap is None:
            return
        if self._sessions.map is not None:
            self._map_seen = True
        if self._periodic_id is None:
            self._periodic_id = self._schedule(self._interval, self._periodic)
        if self._initial_id is None:
            self._initial_id = self._schedule(
                CALL_HISTORY_INITIAL_DELAY_SEC, self._initial,
            )
        if self._deferred and self._sessions.map is not None:
            self.refresh("deferred")

    def request_sync(self, reason: str) -> None:
        """Ask for a prompt pull, e.g. after the phone reports a call ended.

        Requests within ``CALL_HISTORY_REQUEST_DELAY_SEC`` coalesce into one
        pull, a request during a running pull schedules exactly one follow-up,
        and the MAP gating of automatic pulls applies. ``reason`` is logged
        and must not contain personal data.
        """
        if self._stopped:
            return
        log.info("call history sync requested (%s)", reason)
        if self._request_id is not None:
            return
        self._request_id = self._schedule(
            CALL_HISTORY_REQUEST_DELAY_SEC, self._requested,
        )

    def refresh(self, reason: str = "automatic") -> None:
        """Best-effort automatic sync, gated so MAP keeps the worker first."""
        if self._stopped:
            return
        if self._pending:
            if reason == "request":
                self._resync = True
            return
        if self._sessions.pbap is None or not self._storage.status.can_write:
            # PBAP recovery calls profiles_available(), storage recovery
            # calls storage_changed(); either one re-enters here.
            if reason in {"request", "deferred"}:
                self._deferred = True
            return
        if self._sessions.map is not None:
            self._map_seen = True
        elif self._map_seen:
            # MAP was connected and is being re-established. Its retries need
            # the shared worker; catch up once profiles are available again.
            self._defer(reason)
            return
        elif not self._map_wait_finished:
            self._defer(reason)
            if self._map_wait_id is None:
                self._map_wait_id = self._schedule(
                    CALL_HISTORY_MAP_GRACE_SECONDS, self._map_wait_expired,
                )
            return
        elif reason == "periodic":
            # MAP never connected: no background polling, only prompt,
            # requested, and explicit pulls.
            return
        self._deferred = False
        self.sync()

    def _defer(self, reason: str) -> None:
        if not self._deferred:
            log.info("call history sync deferred until MAP is available (%s)", reason)
        self._deferred = True

    def sync(self, success: Success | None = None, failure: Failure | None = None) -> None:
        """Join or start one call-history sync; called and completed on GLib."""
        if success is not None and failure is not None:
            if len(self._waiters) >= MAX_OBEX_PENDING_OPERATIONS:
                failure(RuntimeError("too many pending call history requests"))
                return
            self._waiters.append((success, failure))
        if self._pending:
            return
        if not self._storage.status.can_write:
            # Not a transport problem: do not mark PBAP unhealthy.
            self._finished(
                error=RuntimeError(self._storage.status.detail), transport=False,
            )
            return
        self._pending = True
        revision = self._storage.revision
        # The worker gets its own key buffer; the live one is zeroed in place
        # whenever storage relocks or changes policy.
        storage = self._storage.snapshot()
        pull = self._pull or pull_call_history
        now = self._clock()

        def download() -> ReplaceResult:
            try:
                records = pull(self._sessions)
                try:
                    return CallHistoryRepository(storage).replace(records, now=now)
                except CorruptStorageError:
                    raise
                except Exception as error:
                    raise CallHistoryStorageError(
                        "could not store call history"
                    ) from error
            finally:
                storage.close()

        def failed(error: Exception) -> None:
            if isinstance(error, CorruptStorageError):
                self._storage.fail_closed(str(error))
            self._finished(
                error=error,
                transport=not isinstance(
                    error, CorruptStorageError | CallHistoryStorageError
                ),
            )

        def succeeded(result: ReplaceResult) -> None:
            try:
                count = self._stored(result, revision, now)
            except Exception as error:
                self._finished(error=error, transport=False)
            else:
                self._finished(count=count)

        try:
            self._submit(
                download,
                on_success=succeeded,
                on_error=failed,
            )
        except Exception as error:
            # The worker refused the job (recovery in progress, queue full,
            # shutting down). No PBAP transfer happened, so it is no evidence
            # of a broken PBAP session.
            storage.close()
            self._finished(error=error, transport=False)

    def _stored(self, result: ReplaceResult, revision: int, now: datetime) -> int:
        if self._storage.revision != revision:
            # Sealed under a key or policy that is no longer current. The phone
            # still has the list, so erase and let the next poll pull again.
            clear_call_history()
            self.discard_cache()
            raise StorageChangedDuringCallSync(
                "local storage changed during call history sync"
            )
        self._records = list(result.records)
        self._synced = True
        if result.changed:
            self._on_changed()
        if result.seeded:
            log.info("call history seeded (%d calls); no popups for existing calls",
                     len(result.records))
        fresh = [
            record for record in result.new_missed
            if record.occurred_at >= now - MISSED_CALL_NOTIFY_MAX_AGE
        ]
        if fresh:
            log.info("%d new missed calls", len(fresh))
            try:
                self._on_missed(fresh)
            except Exception:
                log.exception("missed-call notification failed")
        return len(result.records)

    def _finished(
        self, *, count: int = 0, error: Exception | None = None,
        transport: bool = True,
    ) -> None:
        waiters, self._waiters = self._waiters, []
        self._pending = False
        if isinstance(error, StorageChangedDuringCallSync) or not transport:
            log.info("call history sync did not run: %s", error)
        elif error is not None:
            log.error("call history sync failed; keeping previous list: %s", error)
            try:
                self._sessions.report_error(error)
            except Exception:
                log.exception("could not report call history transport failure")
        for success, failure in waiters:
            try:
                if error is None:
                    success(count)
                else:
                    failure(error)
            except Exception:
                log.exception("call history completion callback failed")
        if self._resync:
            self._resync = False
            self.refresh("request")

    def stop(self) -> None:
        self._stopped = True
        for attribute in (
            "_periodic_id", "_initial_id", "_map_wait_id", "_request_id",
        ):
            source = getattr(self, attribute)
            if source is not None:
                try:
                    self._cancel(source)
                except Exception:
                    log.debug("could not remove call history timer", exc_info=True)
                setattr(self, attribute, None)

    def _initial(self) -> bool:
        # Rearm so a later PBAP reconnect also gets one prompt sync.
        self._initial_id = None
        self.refresh("initial")
        return False

    def _map_wait_expired(self) -> bool:
        self._map_wait_id = None
        self._map_wait_finished = True
        if self._deferred:
            self.refresh("grace")
        return False

    def _requested(self) -> bool:
        self._request_id = None
        self.refresh("request")
        return False

    def _periodic(self) -> bool:
        self.refresh("periodic")
        return True
