"""Worker-side storage validation and the cache data committed after it succeeds."""
from __future__ import annotations

from dataclasses import dataclass, field

from blueferry.call_history import CallRecord
from blueferry.call_history_repository import CallHistoryRepository, clear_call_history
from blueferry.call_history_settings import call_history_enabled
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.contacts import ContactsResolver, clear_contact_cache
from blueferry.group_routes import GroupRoutesStore
from blueferry.history import (
    clear_events,
    delete_event_rows,
    minimize_ancs_history,
    prune_events,
    read_event_rows,
    read_events,
    scrub_unprotected_events,
)
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_security import NO_STORAGE, CorruptStorageError, StorageSecurity


@dataclass
class PreparedStorage:
    contacts: ContactsResolver
    historical_ancs: list[dict]
    has_messages: bool
    call_history: list[CallRecord] = field(default_factory=list)


def prepare_storage(storage: StorageSecurity) -> PreparedStorage:
    """Use only the request's private key snapshot, never the live daemon state."""
    StarredThreadsStore(storage.settings_path, storage=storage).migrate()
    ConfirmedGroupsStore(storage.settings_path, storage=storage).migrate()
    routes = GroupRoutesStore(storage.settings_path, storage=storage)
    routes.migrate()
    calls: list[CallRecord] = []
    if storage.status.policy == NO_STORAGE:
        clear_events()
        clear_contact_cache()
        clear_call_history()
    elif storage.status.can_read:
        scrub_unprotected_events(storage=storage)
        # Before pruning: retention must not discard a roster still in use.
        _move_group_routes_out_of_history(storage, routes)
        prune_events(storage=storage)
        minimize_ancs_history(storage=storage)
        calls = _prepare_call_history(storage)
    contacts = ContactsResolver(storage=storage, strict=True)
    historical = read_events(kinds={"ancs_notification"}, limit=2_000, storage=storage)
    has_messages = bool(read_events(kinds={"sms_received"}, limit=1, storage=storage))
    if storage.status.state == "error":
        raise CorruptStorageError(storage.status.detail)
    return PreparedStorage(contacts, historical, has_messages, calls)


def _prepare_call_history(storage: StorageSecurity) -> list[CallRecord]:
    """Apply retention to the call mirror, or erase it once the feature is off."""
    if not call_history_enabled():
        # Turning the opt-in off must not leave who-called-whom on disk.
        clear_call_history()
        return []
    repository = CallHistoryRepository(storage)
    repository.prune()
    return repository.load()


def _move_group_routes_out_of_history(
    storage: StorageSecurity, routes: GroupRoutesStore,
) -> None:
    """Adopt rosters that older releases saved as history events."""
    if not storage.status.can_write:
        return
    legacy = [
        (event_id, event) for event_id, event in read_event_rows(storage=storage)
        if event.get("kind") == "group_route"
    ]
    if not legacy:
        return
    routes.add_missing(event for _event_id, event in legacy)
    delete_event_rows((event_id for event_id, _event in legacy), storage=storage)
