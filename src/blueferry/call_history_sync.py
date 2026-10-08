"""Schedule PBAP call-history pulls and announce newly missed calls.

The blocking part of a sync runs on the shared OBEX worker, one PBAP listing
per job: the next phonebook is submitted only after the previous one
finished, so a MAP send queued meanwhile waits for at most one small
listing, never for a whole three-phonebook sync. The encrypted replacement
write is a final short job. Everything else, including the in-memory snapshot
served to D-Bus clients and the missed-call callback, runs on the GLib loop.

PBAP has no change notification. Pulls are therefore driven by events where
possible: the daemon requests one when ANCS reports a missed call or the end
of an incoming call (content-free category only). The periodic fallback poll
(``BLUEFERRY_CALL_HISTORY_INTERVAL_SEC``) lists only ``mch``, the one
phonebook that missed-call detection needs; ``ich`` and ``och`` are pulled on
the first sync, on request, and when a client asks. Failed automatic pulls
back off exponentially.

Automatic pulls yield to MAP, which shares the single worker (see #165 and
``ContactSync``): while a MAP session that was connected is being
re-established, every automatic pull is deferred until profiles report
availability again. When MAP has never connected in this daemon, automatic
pulls wait for the same three-minute grace period as contact sync and the
periodic tick stays off; only prompt, grace-expiry, and requested pulls run.
Explicit client syncs are never gated.

A call-history failure never tears down the OBEX sessions on a timeout
(``NoReply``): these pulls are optional and frequent, and dropping MAP for
them would cost message delivery. Only definitive "the session object is
gone" errors are reported to the session manager.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from gi.repository import GLib

from blueferry import config
from blueferry import contacts as contacts_module
from blueferry.call_history import (
    MISSED,
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
# a stuck transfer must not hold the shared worker for long. With PBAP's 10 s
# Select and 30 s PullAll call timeouts one job stays well below the clients'
# OBEX_CALL_TIMEOUT_SEC (240 s), so a send queued behind it does not time out.
CALL_HISTORY_TRANSFER_MAX_SECONDS = 45
# Never announce a call that is older than this, even if it was never seen:
# after a long time offline the list is useful, a burst of stale popups is not.
MISSED_CALL_NOTIFY_MAX_AGE = timedelta(hours=12)
# Exponential back-off for failed automatic pulls, in polling intervals.
CALL_HISTORY_MAX_BACKOFF_TICKS = 8

# What one sync pulls: every directional list, or only the missed calls.
FULL_PULL: tuple[tuple[str, str], ...] = PHONEBOOKS
MISSED_PULL: tuple[tuple[str, str], ...] = tuple(
    entry for entry in PHONEBOOKS if entry[1] == MISSED
)
# Only these mean the PBAP session object is gone; see the module docstring.
_SESSION_GONE_MARKERS = ("UnknownObject", "ServiceUnknown", "NameHasNoOwner")

Success = Callable[[int], None]
Failure = Callable[[Exception], None]
Pull = Callable[[Any, str, str], list[CallRecord]]


class StorageChangedDuringCallSync(RuntimeError):
    """Local storage changed policy or key while a sync was writing."""


class CallHistoryStorageError(RuntimeError):
    """The pull worked but the local mirror could not be written."""


class CallHistoryStopped(RuntimeError):
    """The feature was turned off or the daemon is stopping."""


def pull_call_list(
    sessions: SessionManager, phonebook: str, direction: str,
) -> list[CallRecord]:
    """Pull and parse one call-history phonebook. Worker thread only."""
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
    return parsed


def pull_call_history(sessions: SessionManager) -> list[CallRecord]:
    """Pull ich/och/mch individually and merge them. Worker thread only.

    The combined ``cch`` phonebook is deliberately not used: it is reported
    to be incomplete on iOS, and the directional lists carry the same calls.
    The daemon itself submits one job per phonebook (see ``CallHistorySync``).
    """
    return merge_call_history(
        pull_call_list(sessions, phonebook, direction)
        for phonebook, direction in PHONEBOOKS
    )


class CallHistorySync:
    """Own the call-history snapshot, its timers, and the joined manual sync."""

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
        phone: str | None = None,
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
        self._phone = config.IPHONE_MAC if phone is None else phone
        self._records: list[CallRecord] = []
        self._cache_generation = 0
        self._store_lock = threading.Lock()
        self._pending = False
        self._pending_full = False
        self._waiters: list[tuple[Success, Failure]] = []
        # Explicit callers that arrived while a missed-calls-only pull ran:
        # they are owed a full pull, which starts right after it.
        self._queued: list[tuple[Success, Failure]] = []
        self._periodic_id: int | None = None
        self._initial_id: int | None = None
        self._stopped = False
        self._erase_when_stopped = False
        self._synced = False
        # MAP gating (mirrors ContactSync; see module docstring).
        self._deferred = False
        self._map_seen = False
        self._map_wait_id: int | None = None
        self._map_wait_finished = False
        # request_sync(): one coalescing timer, and one follow-up pull when a
        # request arrives while a pull (possibly started too early) runs.
        self._request_id: int | None = None
        self._request_full = False
        self._resync = False
        self._resync_full = False
        # Back-off for failed automatic pulls, counted in periodic ticks.
        self._failures = 0
        self._skip_ticks = 0

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
        with self._store_lock:
            self._cache_generation += 1
        if self._records:
            self._records = []
            self._on_changed()

    def clear(self) -> None:
        """Erase history and invalidate pulls and writes already in flight."""
        # A worker may be committing when ClearHistory arrives. Wait for that
        # commit before erasing, and prevent queued old writes from following.
        with self._store_lock:
            self._cache_generation += 1
            clear_call_history()
        self._synced = False
        self._resync = False
        self._resync_full = False
        if self._request_id is not None:
            try:
                self._cancel(self._request_id)
            except Exception:
                log.debug("could not remove call history request timer", exc_info=True)
            self._request_id = None
        self._request_full = False
        queued, self._queued = self._queued, []
        for _success, failure in queued:
            try:
                failure(StorageChangedDuringCallSync("call history was cleared"))
            except Exception:
                log.exception("call history completion callback failed")
        if self._records:
            self._records = []
            self._on_changed()

    def forget_phone(self) -> None:
        """The bond was removed: the next sync seeds silently again."""
        try:
            CallHistoryRepository(None).forget_announcements()
        except Exception:
            log.exception("could not reset missed-call announcements")

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

    def request_sync(self, reason: str, *, full: bool = True) -> None:
        """Ask for a prompt pull, e.g. after ANCS reports a missed call.

        Requests within ``CALL_HISTORY_REQUEST_DELAY_SEC`` coalesce into one
        pull (a full pull if any of them asked for one), a request during a
        running pull schedules exactly one follow-up, and the MAP gating of
        automatic pulls applies. ``reason`` is logged and must not contain
        personal data.
        """
        if self._stopped:
            return
        log.info("call history sync requested (%s)", reason)
        self._request_full = self._request_full or full
        if self._request_id is not None:
            return
        self._request_id = self._schedule(
            CALL_HISTORY_REQUEST_DELAY_SEC, self._requested,
        )

    def refresh(self, reason: str = "automatic", *, full: bool = True) -> None:
        """Best-effort automatic sync, gated so MAP keeps the worker first."""
        if self._stopped:
            return
        if self._pending:
            if reason == "request":
                self._resync = True
                self._resync_full = self._resync_full or full
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
        # Until one full sync succeeded there is no incoming/outgoing list to
        # keep, so the first automatic pull is always a full one.
        self._start(full=full or not self._synced)

    def _defer(self, reason: str) -> None:
        if not self._deferred:
            log.info("call history sync deferred until MAP is available (%s)", reason)
        self._deferred = True

    def sync(self, success: Success | None = None, failure: Failure | None = None) -> None:
        """Join or start one full call-history sync (explicit client request)."""
        if success is not None and failure is not None:
            if len(self._waiters) + len(self._queued) >= MAX_OBEX_PENDING_OPERATIONS:
                failure(RuntimeError("too many pending call history requests"))
                return
            if self._pending and not self._pending_full:
                # A missed-calls-only poll is running; the caller asked for
                # the whole list, so it gets the full pull that follows.
                self._queued.append((success, failure))
                return
            self._waiters.append((success, failure))
        if self._pending:
            return
        self._start(full=True)

    def _start(self, *, full: bool) -> None:
        if self._stopped:
            self._finished(error=CallHistoryStopped("call history is off"), transport=False)
            return
        if not self._storage.status.can_write:
            # Not a transport problem: do not mark PBAP unhealthy.
            self._finished(
                error=RuntimeError(self._storage.status.detail), transport=False,
            )
            return
        self._pending = True
        self._pending_full = full
        plan = FULL_PULL if full else MISSED_PULL
        revision = self._storage.revision
        cache_generation = self._cache_generation
        # The worker gets its own key buffer; the live one is zeroed in place
        # whenever storage relocks or changes policy. ``follow`` makes the
        # copy refuse to seal once the policy or key changed mid-sync.
        storage = self._storage.snapshot(follow=True)
        pull = self._pull or pull_call_list
        now = self._clock()
        collected: list[list[CallRecord]] = []

        def abort(error: Exception, *, transport: bool) -> None:
            storage.close()
            self._finished(error=error, transport=transport)

        def submit(operation: Callable[[], Any], done: Callable[[Any], None]) -> None:
            try:
                self._submit(operation, on_success=done, on_error=failed)
            except Exception as error:
                # The worker refused the job (recovery in progress, queue
                # full, shutting down). No PBAP transfer happened, so it is
                # no evidence of a broken PBAP session.
                abort(error, transport=False)

        def failed(error: Exception) -> None:
            if isinstance(error, CorruptStorageError):
                self._storage.fail_closed(str(error))
            abort(error, transport=not isinstance(
                error, CorruptStorageError | CallHistoryStorageError,
            ))

        def pull_next() -> None:
            if cache_generation != self._cache_generation:
                abort(StorageChangedDuringCallSync("call history was cleared"), transport=False)
                return
            if self._stopped:
                abort(CallHistoryStopped("call history is off"), transport=False)
                return
            phonebook, direction = plan[len(collected)]
            submit(lambda: pull(self._sessions, phonebook, direction), pulled)

        def pulled(records: list[CallRecord]) -> None:
            collected.append(list(records))
            if len(collected) < len(plan):
                pull_next()
                return
            if self._stopped:
                abort(CallHistoryStopped("call history is off"), transport=False)
                return
            submit(store, stored)

        def store() -> ReplaceResult:
            try:
                with self._store_lock:
                    if cache_generation != self._cache_generation:
                        raise StorageChangedDuringCallSync("call history was cleared")
                    return CallHistoryRepository(storage).replace(
                        merge_call_history(collected),
                        now=now,
                        phone=self._phone or None,
                        directions=frozenset(direction for _phonebook, direction in plan),
                    )
            except (CorruptStorageError, StorageChangedDuringCallSync):
                raise
            except Exception as error:
                raise CallHistoryStorageError("could not store call history") from error
            finally:
                storage.close()

        def stored(result: ReplaceResult) -> None:
            try:
                if cache_generation != self._cache_generation:
                    raise StorageChangedDuringCallSync("call history was cleared")
                count = self._stored(result, revision, now, full=full)
            except Exception as error:
                self._finished(error=error, transport=False)
            else:
                self._finished(count=count)

        pull_next()

    def _stored(
        self, result: ReplaceResult, revision: int, now: datetime, *, full: bool,
    ) -> int:
        if self._storage.revision != revision:
            # Sealed under a key or policy that is no longer current. The phone
            # still has the list, so erase and let the next poll pull again.
            clear_call_history()
            self.discard_cache()
            raise StorageChangedDuringCallSync(
                "local storage changed during call history sync"
            )
        if self._stopped:
            if self._erase_when_stopped:
                # Turned off while the write ran: do not keep what it wrote.
                clear_call_history()
            self.discard_cache()
            raise CallHistoryStopped("call history is off")
        self._records = list(result.records)
        if full:
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

    def _report_transport(self, error: Exception) -> None:
        text = str(error)
        if not any(marker in text for marker in _SESSION_GONE_MARKERS):
            # Includes NoReply: a slow listing is no reason to drop MAP.
            return
        try:
            self._sessions.report_error(error)
        except Exception:
            log.exception("could not report call history transport failure")

    def _finished(
        self, *, count: int = 0, error: Exception | None = None,
        transport: bool = True,
    ) -> None:
        waiters, self._waiters = self._waiters, []
        self._pending = False
        self._pending_full = False
        if error is None:
            self._failures = 0
            self._skip_ticks = 0
        elif isinstance(error, StorageChangedDuringCallSync | CallHistoryStopped) or not transport:
            log.info("call history sync did not run: %s", error)
        else:
            self._failures += 1
            self._skip_ticks = min(
                2 ** self._failures - 1, CALL_HISTORY_MAX_BACKOFF_TICKS,
            )
            log.error("call history sync failed; keeping previous list: %s", error)
            self._report_transport(error)
        for success, failure in waiters:
            try:
                if error is None:
                    success(count)
                else:
                    failure(error)
            except Exception:
                log.exception("call history completion callback failed")
        if self._queued:
            queued, self._queued = self._queued, []
            self._waiters.extend(queued)
            self._resync = False
            self._start(full=True)
        elif self._resync:
            full, self._resync, self._resync_full = self._resync_full, False, False
            self.refresh("request", full=full)

    def disable(self) -> None:
        """The user turned the feature off: stop, and erase a late write.

        The caller erases the mirror itself; this only makes sure a listing
        or store job still in flight cannot leave records behind.
        """
        self._erase_when_stopped = True
        self.stop()

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
        full, self._request_full = self._request_full, False
        self.refresh("request", full=full)
        return False

    def _periodic(self) -> bool:
        if self._skip_ticks > 0:
            # Back-off after a failed pull; event-driven requests still run.
            self._skip_ticks -= 1
            return True
        self.refresh("periodic", full=False)
        return True
