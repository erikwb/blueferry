"""Worker-side storage validation and the cache data committed after it succeeds."""
from __future__ import annotations

from dataclasses import dataclass

from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.contacts import ContactsResolver, clear_contact_cache
from blueferry.history import (
    clear_events,
    minimize_ancs_history,
    prune_events,
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


def prepare_storage(storage: StorageSecurity) -> PreparedStorage:
    """Use only the request's private key snapshot, never the live daemon state."""
    StarredThreadsStore(storage.settings_path, storage=storage).migrate()
    ConfirmedGroupsStore(storage.settings_path, storage=storage).migrate()
    if storage.status.policy == NO_STORAGE:
        clear_events()
        clear_contact_cache()
    elif storage.status.can_read:
        scrub_unprotected_events(storage=storage)
        prune_events(storage=storage)
        minimize_ancs_history(storage=storage)
    contacts = ContactsResolver(storage=storage, strict=True)
    historical = read_events(kinds={"ancs_notification"}, limit=2_000, storage=storage)
    has_messages = bool(read_events(kinds={"sms_received"}, limit=1, storage=storage))
    if storage.status.state == "error":
        raise CorruptStorageError(storage.status.detail)
    return PreparedStorage(contacts, historical, has_messages)
