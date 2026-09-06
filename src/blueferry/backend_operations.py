"""Toolkit- and transport-neutral backend application operations.

The D-Bus service adapts these methods to wire types. Bluetooth I/O remains
asynchronous: accepted operations complete through success/error callbacks.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from blueferry.contacts import clear_contact_cache
from blueferry.errors import (
    ConfirmationRequiredError,
    InvalidArgumentsError,
    NotFoundError,
    NotReadyError,
    OperationFailedError,
)
from blueferry.events import canonical_address
from blueferry.grouping import (
    CORRELATED_ANCS_ROW_IDS_FIELD,
    HISTORY_ROW_ID_FIELD,
    correlate_group_events,
)
from blueferry.history import (
    append_event,
    clear_events,
    delete_event_rows,
    history_revision,
    mark_event_handles_read,
    read_event_rows,
    read_events,
)
from blueferry.limits import (
    MAX_CONTACT_ADDRESS_CHARS,
    MAX_CONTACT_PAGE,
    MAX_CONTACT_QUERY_CHARS,
    MAX_CONTACT_RESULTS,
    MAX_CONVERSATION_EVENTS,
    MAX_EVENT_KIND_CHARS,
    MAX_EVENT_KINDS,
    MAX_EVENT_QUERY_LIMIT,
    MAX_OUTGOING_BODY_BYTES,
    MAX_RECENT_QUERY_LIMIT,
    MAX_THREAD_BODY_CHARS,
    MAX_THREAD_DELETE_COUNT,
    MAX_THREAD_KEY_CHARS,
    MAX_THREAD_QUERY_LIMIT,
)
from blueferry.obex.map_query import list_recent_messages
from blueferry.obex.map_read import set_session_messages_read
from blueferry.obex.map_send import send_group_message, send_message
from blueferry.recipients import InvalidRecipient, validate_recipient
from blueferry.storage_security import STORAGE_POLICIES, StorageSecurity
from blueferry.threads import (
    MESSAGE_KINDS,
    ConversationIndex,
    build_threads,
    group_confirmation_token,
    sort_threads,
    thread_key,
)

log = logging.getLogger(__name__)

_MAP_FOLDER_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PUBLIC_EVENT_KINDS = frozenset({"sms_received", "sms_sent", "sms_seen"})

Success = Callable[[Any], None]
Failure = Callable[[Exception], None]
Operation = Callable[[], Any]


class SessionState(Protocol):
    @property
    def map(self) -> object | None: ...

    @property
    def pbap(self) -> object | None: ...

    @property
    def map_path(self) -> str: ...

    def report_error(self, error: Exception) -> None: ...


class ContactIndex(Protocol):
    def find_by_name(self, query: str) -> list[tuple[str, str]]: ...

    def records(
        self, offset: int = 0, limit: int | None = None
    ) -> list[tuple[str | None, list[str], list[str]]]: ...

    def resolve(self, address: str | None) -> str | None: ...

    def refresh(self) -> int: ...


class NotificationPolicy(Protocol):
    @property
    def value(self) -> str: ...

    @property
    def contacts_only(self) -> bool: ...

    def set(self, value: str) -> str: ...

    def set_contacts_only(self, enabled: bool) -> bool: ...


class StarredThreads(Protocol):
    def keys(self) -> Sequence[str]: ...

    def set_starred(self, thread_key: str, starred: bool) -> bool: ...

    def discard(self, thread_keys: Sequence[str]) -> None: ...

    def clear(self) -> None: ...


class ConfirmedGroups(Protocol):
    def matches(self, thread_key: str, token: str) -> bool: ...

    def remember(self, thread_key: str, token: str) -> None: ...

    def forget(self, thread_keys: Sequence[str]) -> None: ...

    def clear(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendDependencies:
    """Explicit optional capabilities supplied by the daemon composition root."""

    submit_obex: Callable[..., object] | None = None
    on_sent: Callable[[str, str, str], None] | None = None
    on_group_sent: Callable[..., None] | None = None
    pull_contacts: Callable[[], int] | None = None
    on_contacts_pulled: Callable[[int], int] | None = None
    contacts: ContactIndex | None = None
    status_provider: Callable[[], dict[str, Any]] | None = None
    notification_policy: NotificationPolicy | None = None
    on_notification_policy_changed: Callable[[], None] | None = None
    starred_threads: StarredThreads | None = None
    confirmed_groups: ConfirmedGroups | None = None
    storage: StorageSecurity | None = None
    on_storage_changed: Callable[[], None] | None = None


class BackendOperations:
    """Own validation, reply routing, and backend operation policy."""

    def __init__(
        self,
        sessions: SessionState,
        dependencies: BackendDependencies | None = None,
    ) -> None:
        self.sessions = sessions
        self.dependencies = dependencies or BackendDependencies()
        self._confirmed_groups: dict[str, str] = {}
        self._conversations = ConversationIndex(
            lambda: read_events(
                limit=MAX_CONVERSATION_EVENTS,
                max_body_chars=MAX_THREAD_BODY_CHARS,
                storage=self.dependencies.storage,
            ),
            lambda events: build_threads(events, self.dependencies.contacts),
            revision=lambda: history_revision(storage=self.dependencies.storage),
        )

    def _require_map(self) -> None:
        if self.sessions.map is None:
            raise NotReadyError(
                "MAP session not open — check Show Message Notifications on the iPhone"
            )

    @staticmethod
    def _validate_body(body: str) -> str:
        if not body.strip():
            raise InvalidArgumentsError("message body must be non-empty")
        if len(body.encode("utf-8")) > MAX_OUTGOING_BODY_BYTES:
            raise InvalidArgumentsError(
                f"message body exceeds {MAX_OUTGOING_BODY_BYTES} UTF-8 bytes"
            )
        return body

    def _queue(
        self, operation: Operation, success: Success, failure: Failure
    ) -> None:
        if self.dependencies.submit_obex is None:
            failure(NotReadyError("OBEX worker is unavailable"))
            return
        try:
            self.dependencies.submit_obex(
                operation, on_success=success, on_error=failure
            )
        except Exception as error:
            failure(error)

    def _operation_failed(self, operation: str, error: Exception, failure: Failure) -> None:
        log.error("%s failed: %s", operation, error)
        try:
            self.sessions.report_error(error)
        except Exception:
            log.exception("could not update OBEX session state after failure")
        failure(OperationFailedError(operation, error))

    def _queue_send(self, recipient: str, body: str, success: Success, failure: Failure) -> None:
        map_path = self.sessions.map_path

        def succeeded(transfer: str) -> None:
            if self.dependencies.on_sent is not None:
                try:
                    self.dependencies.on_sent(recipient, body, transfer)
                except Exception:
                    log.exception("on_sent hook failed (message was still sent)")
            success(transfer)

        self._queue(
            lambda: send_message(map_path, recipient, body),
            succeeded,
            lambda error: self._operation_failed("Send", error, failure),
        )

    def send(self, recipient: str, body: str, success: Success, failure: Failure) -> None:
        if not recipient.strip():
            raise InvalidArgumentsError("recipient must be non-empty")
        self._validate_body(body)
        try:
            normalized = validate_recipient(recipient)
        except InvalidRecipient as error:
            raise InvalidArgumentsError(str(error)) from error
        self._require_map()
        self._queue_send(normalized, body, success, failure)

    def send_to_thread(
        self,
        thread_key: str,
        body: str,
        confirm_group: bool,
        success: Success,
        failure: Failure,
    ) -> None:
        if not thread_key.strip():
            raise InvalidArgumentsError("thread key must be non-empty")
        if len(thread_key) > 1024:
            raise InvalidArgumentsError("thread key is too long")
        self._validate_body(body)
        thread = self._conversations.find(thread_key)
        if thread is None:
            raise NotFoundError("thread no longer exists in local history")
        if not thread["reply_ready"] or not thread["recipients"]:
            raise NotReadyError("thread has no unambiguous reply destination")
        self._require_map()

        if thread["is_group"]:
            try:
                group_recipients = [
                    validate_recipient(str(value))
                    for value in thread["recipients"]
                ]
            except InvalidRecipient as error:
                raise NotReadyError(f"thread has an invalid group route: {error}") from error
            if not 2 <= len(group_recipients) <= 20:
                raise NotReadyError("group thread must have 2 to 20 recipients")
            if len(set(group_recipients)) != len(group_recipients):
                raise NotReadyError("group thread contains duplicate recipients")
            token = group_confirmation_token(
                group_recipients,
                thread.get("roster_warning_id"),
            )
            if not confirm_group and not self._group_roster_confirmed(
                thread_key, token
            ):
                raise ConfirmationRequiredError(
                    "confirm the displayed participant list before replying "
                    "to this group"
                )

            def succeeded(transfer: str) -> None:
                self._remember_confirmed_group(thread_key, token)
                if self.dependencies.on_group_sent is not None:
                    try:
                        self.dependencies.on_group_sent(
                            group_recipients, thread["key"],
                            thread["name"], body, transfer,
                            thread["members"],
                        )
                    except Exception:
                        log.exception("on_group_sent hook failed (message was still sent)")
                success(transfer)

            map_path = self.sessions.map_path
            self._queue(
                lambda: send_group_message(map_path, group_recipients, body),
                succeeded,
                lambda error: self._operation_failed("Send", error, failure),
            )
            return

        if len(thread["recipients"]) != 1:
            raise NotReadyError("one-to-one thread has an invalid recipient set")
        self._queue_send(thread["recipients"][0], body, success, failure)

    def list_events(self, kinds: Iterable[object], limit: int) -> list[dict]:
        raw_kinds = list(kinds)
        if len(raw_kinds) > MAX_EVENT_KINDS:
            raise InvalidArgumentsError("too many event kinds")
        selected: set[str] = set()
        for kind in raw_kinds:
            value = str(kind)
            if (
                len(value) > MAX_EVENT_KIND_CHARS
                or not _EVENT_KIND_RE.fullmatch(value)
                or value not in _PUBLIC_EVENT_KINDS
            ):
                raise InvalidArgumentsError("invalid event kind")
            selected.add(value)
        bounded = max(1, min(int(limit), MAX_EVENT_QUERY_LIMIT))
        return read_events(
            kinds=selected or set(_PUBLIC_EVENT_KINDS),
            limit=bounded,
            max_body_chars=MAX_THREAD_BODY_CHARS,
            storage=self.dependencies.storage,
        )

    def list_threads(self, limit: int) -> list[dict]:
        bounded = max(1, min(int(limit), MAX_THREAD_QUERY_LIMIT))
        return self._present_threads(self._conversations.threads())[:bounded]

    def _starred_keys(self) -> set[str]:
        store = self.dependencies.starred_threads
        if store is None:
            return set()
        return {str(key) for key in store.keys()}

    def _group_roster_confirmed(self, thread_key: str, token: str) -> bool:
        if self._confirmed_groups.get(thread_key) == token:
            return True
        store = self.dependencies.confirmed_groups
        return store is not None and store.matches(thread_key, token)

    def _remember_confirmed_group(self, thread_key: str, token: str) -> None:
        self._confirmed_groups[thread_key] = token
        store = self.dependencies.confirmed_groups
        if store is None:
            return
        try:
            store.remember(thread_key, token)
        except (OSError, ValueError, RuntimeError):
            log.exception("could not persist confirmed group roster")

    def _forget_confirmed_groups(self, thread_keys: Iterable[str]) -> None:
        selected = [str(key) for key in thread_keys]
        for key in selected:
            self._confirmed_groups.pop(key, None)
        store = self.dependencies.confirmed_groups
        if store is not None:
            store.forget(selected)

    def _clear_confirmed_groups(self) -> None:
        self._confirmed_groups.clear()
        store = self.dependencies.confirmed_groups
        if store is not None:
            store.clear()

    def _group_confirmed(self, thread: dict) -> bool:
        if not thread.get("is_group"):
            return False
        token = group_confirmation_token(
            thread.get("recipients") or [],
            thread.get("roster_warning_id"),
        )
        return self._group_roster_confirmed(str(thread.get("key") or ""), token)

    def _present_threads(self, threads: Sequence[dict]) -> list[dict]:
        presented = sort_threads(threads, starred_keys=self._starred_keys())
        for item in presented:
            item["group_confirmed"] = self._group_confirmed(item)
        return presented

    def set_thread_starred(self, thread_key: str, starred: bool) -> bool:
        if not thread_key.strip() or len(thread_key) > MAX_THREAD_KEY_CHARS:
            raise InvalidArgumentsError("invalid thread key")
        if self.dependencies.starred_threads is None:
            raise NotReadyError("starred conversations are unavailable")
        thread = self._conversations.find(thread_key)
        if thread is None:
            raise NotFoundError("thread no longer exists in local history")
        try:
            return self.dependencies.starred_threads.set_starred(
                thread_key, bool(starred)
            )
        except ValueError as error:
            raise InvalidArgumentsError(str(error)) from error

    def mark_thread_read(self, thread_key: str) -> int:
        """Mark incoming messages in one conversation read locally and on MAP."""
        if not thread_key.strip() or len(thread_key) > 1024:
            raise InvalidArgumentsError("invalid thread key")
        thread = self._conversations.find(thread_key)
        if thread is None:
            raise NotFoundError("thread no longer exists in local history")
        handles = [
            str(message.get("handle") or "")
            for message in thread.get("messages") or []
            if not message.get("outgoing")
            and not message.get("read")
            and message.get("handle")
        ]
        handles = [handle for handle in handles if handle]
        if not handles:
            return 0
        storage = self.dependencies.storage
        if storage is not None and not storage.status.can_write:
            raise NotReadyError(storage.status.detail)
        updated = mark_event_handles_read(handles, storage=storage)
        self._conversations.invalidate()
        if self.sessions.map is None:
            return updated
        map_path = self.sessions.map_path
        self._queue(
            lambda: set_session_messages_read(map_path, handles),
            lambda _result: None,
            lambda error: log.debug("MAP mark-read failed: %s", error),
        )
        return updated

    def find_contacts(self, query: str) -> list[dict[str, str]]:
        selected = str(query).strip()
        if not selected:
            return []
        if len(selected) > MAX_CONTACT_QUERY_CHARS:
            raise InvalidArgumentsError("contact query is too long")
        if self.dependencies.contacts is None:
            raise NotReadyError("contact cache is unavailable")
        return [
            {"name": name, "address": address}
            for name, address in self.dependencies.contacts.find_by_name(selected)[
                :MAX_CONTACT_RESULTS
            ]
        ]

    def list_contacts(self, offset: int, limit: int) -> list[dict[str, object]]:
        """Enumerate the cached phonebook one bounded page at a time.

        Search cannot answer "who is in the phonebook" — a caller that wants
        the whole set would have to guess queries. Each record keeps its
        addresses grouped, so one person stays one record.
        """
        if self.dependencies.contacts is None:
            raise NotReadyError("contact cache is unavailable")
        bounded = max(1, min(int(limit), MAX_CONTACT_PAGE))
        return [
            {"name": name or "", "phones": list(phones), "emails": list(emails)}
            for name, phones, emails in self.dependencies.contacts.records(
                max(0, int(offset)), bounded
            )
        ]

    def set_group_participants(
        self, thread_key: str, recipients: Sequence[object]
    ) -> dict:
        """Persist an explicit reply roster for one named iOS group."""
        if not thread_key.strip() or len(thread_key) > 1024:
            raise InvalidArgumentsError("invalid thread key")
        thread = self._conversations.find(thread_key)
        if thread is None:
            raise NotFoundError("thread no longer exists in local history")
        if not thread.get("is_group") or thread.get("group_origin") != "named":
            raise InvalidArgumentsError(
                "participants can only be supplied for a named group"
            )

        if isinstance(recipients, str | bytes) or not 2 <= len(recipients) <= 20:
            raise InvalidArgumentsError(
                "a group requires 2 to 20 recipients other than you"
            )
        supplied = [str(value) for value in recipients]
        if any(len(value) > MAX_CONTACT_ADDRESS_CHARS for value in supplied):
            raise InvalidArgumentsError("a group recipient is too long")
        try:
            normalized = [validate_recipient(value) for value in supplied]
        except InvalidRecipient as error:
            raise InvalidArgumentsError(str(error)) from error
        identities = [canonical_address(value) for value in normalized]
        if len(set(identities)) != len(identities):
            raise InvalidArgumentsError("group recipients must be unique")

        observed = {
            canonical_address(str(value))
            for value in thread.get("observed_recipients", [])
        }
        observed.discard(None)
        if not observed.issubset(set(identities)):
            raise InvalidArgumentsError(
                "the participant list must include every observed sender"
            )
        storage = self.dependencies.storage
        if storage is not None and not storage.status.can_write:
            raise NotReadyError(storage.status.detail)

        members: list[str] = []
        for recipient in normalized:
            name = (
                self.dependencies.contacts.resolve(recipient)
                if self.dependencies.contacts is not None else None
            )
            members.append(str(name or recipient))
        route = {
            "kind": "group_route",
            "group_key": thread_key,
            "group_name": str(thread["name"]),
            "group_members": members,
            "group_recipients": normalized,
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            append_event(route, storage=storage)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            log.error("could not retain named group participants: %s", error)
            raise NotReadyError(
                "could not retain the group participant list"
            ) from error
        self._forget_confirmed_groups([thread_key])
        self.invalidate_conversations()
        updated = self._conversations.find(thread_key)
        if updated is None:
            raise NotFoundError("thread no longer exists in local history")
        return self._present_threads([updated])[0]

    def invalidate_conversations(self) -> None:
        self._conversations.invalidate()

    def status(self) -> dict:
        status = {
            "daemon": True,
            "map": self.sessions.map is not None,
            "pbap": self.sessions.pbap is not None,
            "notification_policy": self.get_notification_policy(),
            "contacts_only_notifications": (
                self.get_contacts_only_notifications()
            ),
        }
        if self.dependencies.status_provider is not None:
            status.update(self.dependencies.status_provider())
        return status

    def clear_history(self, confirmed: bool) -> None:
        if not confirmed:
            raise ConfirmationRequiredError(
                "history deletion requires explicit confirmation"
            )
        clear_events()
        if self.dependencies.starred_threads is not None:
            self.dependencies.starred_threads.clear()
        self._clear_confirmed_groups()
        self.invalidate_conversations()

    def delete_threads(
        self, thread_keys: Sequence[object], confirmed: bool
    ) -> int:
        """Erase complete local conversations resolved from opaque keys."""
        if not confirmed:
            raise ConfirmationRequiredError(
                "conversation deletion requires explicit confirmation"
            )
        if isinstance(thread_keys, str | bytes):
            raise InvalidArgumentsError("thread keys must be an array")
        if not 1 <= len(thread_keys) <= MAX_THREAD_DELETE_COUNT:
            raise InvalidArgumentsError(
                f"select 1 to {MAX_THREAD_DELETE_COUNT} conversations"
            )
        selected: list[str] = []
        for raw_key in thread_keys:
            key = str(raw_key).strip()
            if not key or len(key) > 1024:
                raise InvalidArgumentsError("invalid thread key")
            if key not in selected:
                selected.append(key)

        current_keys = {
            str(thread["key"]) for thread in self._conversations.threads()
        }
        if not set(selected).issubset(current_keys):
            raise NotFoundError(
                "a selected thread no longer exists in local history"
            )

        storage = self.dependencies.storage
        if storage is not None and not storage.status.can_write:
            raise NotReadyError(storage.status.detail)
        try:
            # Anchor the visible projection before replaying the full archive.
            recent_rows = read_event_rows(
                limit=MAX_CONVERSATION_EVENTS, storage=storage
            )
            recent = correlate_group_events([
                {
                    **event,
                    HISTORY_ROW_ID_FIELD: event_id,
                    CORRELATED_ANCS_ROW_IDS_FIELD: [],
                }
                for event_id, event in recent_rows
            ], self.dependencies.contacts)
            anchor_keys: dict[int, str] = {}
            recent_evidence_ids: set[int] = set()
            for event in recent:
                projected_key = thread_key(event)
                if event.get("kind") not in MESSAGE_KINDS:
                    continue
                if projected_key is None or projected_key not in selected:
                    continue
                anchor_keys[int(event[HISTORY_ROW_ID_FIELD])] = projected_key
                recent_evidence_ids.update(
                    correlated_id
                    for correlated_id in event.get(
                        CORRELATED_ANCS_ROW_IDS_FIELD, []
                    )
                    if isinstance(correlated_id, int)
                )

            all_rows = read_event_rows(storage=storage)
            correlated = correlate_group_events([
                {
                    **event,
                    HISTORY_ROW_ID_FIELD: event_id,
                    CORRELATED_ANCS_ROW_IDS_FIELD: [],
                }
                for event_id, event in all_rows
            ], self.dependencies.contacts)
            resolved_keys = set(selected)
            for event in correlated:
                event_id = int(event.get(HISTORY_ROW_ID_FIELD, -1))
                anchored_key = anchor_keys.get(event_id)
                if anchored_key is None:
                    continue
                resolved_key = thread_key(event)
                if (
                    anchored_key.startswith("group:")
                    and resolved_key is not None
                    and resolved_key.startswith("group:")
                ):
                    resolved_keys.add(resolved_key)
            selected_messages = [
                event
                for event in correlated
                if event.get("kind") in MESSAGE_KINDS
                and (
                    int(event.get(HISTORY_ROW_ID_FIELD, -1)) in anchor_keys
                    or thread_key(event) in resolved_keys
                )
            ]
            event_ids = recent_evidence_ids | {
                int(event[HISTORY_ROW_ID_FIELD])
                for event in selected_messages
            }
            selected_handles = {
                str(event.get("handle") or "")
                for event in selected_messages
                if event.get("handle")
            }
            for event in selected_messages:
                event_ids.update(
                    correlated_id
                    for correlated_id in event.get(
                        CORRELATED_ANCS_ROW_IDS_FIELD, []
                    )
                    if isinstance(correlated_id, int)
                )
            for event in correlated:
                row_id = event.get(HISTORY_ROW_ID_FIELD)
                if not isinstance(row_id, int):
                    continue
                kind = str(event.get("kind") or "")
                belongs = (
                    kind == "group_route"
                    and str(event.get("group_key") or "") in resolved_keys
                ) or (
                    kind == "sms_seen"
                    and (
                        thread_key(event) in resolved_keys
                        or str(event.get("handle") or "") in selected_handles
                    )
                )
                if not belongs:
                    continue
                event_ids.add(row_id)
            if not event_ids:
                raise NotFoundError(
                    "selected conversations no longer exist in local history"
                )
            if delete_event_rows(event_ids, storage=storage) == 0:
                raise NotFoundError(
                    "selected conversations no longer exist in local history"
                )
        except NotFoundError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            log.error("could not delete conversations: %s", error)
            raise NotReadyError("could not delete local conversations") from error

        self._forget_confirmed_groups(resolved_keys)
        if self.dependencies.starred_threads is not None:
            self.dependencies.starred_threads.discard(selected)
        self.invalidate_conversations()
        return len(selected)

    def get_storage_policy(self) -> str:
        if self.dependencies.storage is None:
            return "encrypted"
        return self.dependencies.storage.status.policy

    def set_storage_policy(self, value: str) -> dict:
        if self.dependencies.storage is None:
            raise NotReadyError("local storage is unavailable")
        selected = str(value).strip().casefold()
        if selected not in STORAGE_POLICIES:
            choices = ", ".join(sorted(STORAGE_POLICIES))
            raise InvalidArgumentsError(
                f"local data policy must be one of: {choices}"
            )
        if selected != self.dependencies.storage.status.policy:
            # Never mix plaintext and ciphertext in one archive. Clear before
            # changing policy so a crash can leave stale settings or an empty
            # archive, but never private data under the wrong policy.
            clear_events()
            clear_contact_cache()
            if self.dependencies.starred_threads is not None:
                self.dependencies.starred_threads.clear()
            self._clear_confirmed_groups()
        try:
            status = self.dependencies.storage.set_policy(
                selected, allow_prompt=True
            )
        except ValueError as error:
            raise InvalidArgumentsError(str(error)) from error
        if status.policy == "none":
            if self.dependencies.contacts is not None:
                self.dependencies.contacts.refresh()
        elif status.can_read:
            if self.dependencies.contacts is not None:
                self.dependencies.contacts.refresh()
        self.invalidate_conversations()
        if self.dependencies.on_storage_changed is not None:
            self.dependencies.on_storage_changed()
        return {
            "storage_policy": status.policy,
            "storage_state": status.state,
            "storage_detail": status.detail,
        }

    def unlock_storage(self) -> dict:
        if self.dependencies.storage is None:
            raise NotReadyError("local storage is unavailable")
        status = self.dependencies.storage.refresh(allow_prompt=True)
        if status.can_read:
            if self.dependencies.contacts is not None:
                self.dependencies.contacts.refresh()
            self.invalidate_conversations()
        if self.dependencies.on_storage_changed is not None:
            self.dependencies.on_storage_changed()
        return {
            "storage_policy": status.policy,
            "storage_state": status.state,
            "storage_detail": status.detail,
        }

    def get_notification_policy(self) -> str:
        if self.dependencies.notification_policy is None:
            return "messages"
        return str(self.dependencies.notification_policy.value)

    def set_notification_policy(self, value: str) -> str:
        if self.dependencies.notification_policy is None:
            raise NotReadyError("notification policy storage is unavailable")
        try:
            selected = self.dependencies.notification_policy.set(value)
        except ValueError as error:
            raise InvalidArgumentsError(str(error)) from error
        if self.dependencies.on_notification_policy_changed is not None:
            self.dependencies.on_notification_policy_changed()
        return selected

    def get_contacts_only_notifications(self) -> bool:
        if self.dependencies.notification_policy is None:
            return False
        return bool(self.dependencies.notification_policy.contacts_only)

    def set_contacts_only_notifications(self, enabled: bool) -> bool:
        if self.dependencies.notification_policy is None:
            raise NotReadyError("notification policy storage is unavailable")
        try:
            selected = self.dependencies.notification_policy.set_contacts_only(
                enabled
            )
        except ValueError as error:
            raise InvalidArgumentsError(str(error)) from error
        if self.dependencies.on_notification_policy_changed is not None:
            self.dependencies.on_notification_policy_changed()
        return selected

    def list_recent(
        self, folder: str, limit: int, success: Success, failure: Failure
    ) -> None:
        self._require_map()
        selected_folder = folder or "telecom/msg/INBOX"
        if len(selected_folder) > 128 or not _MAP_FOLDER_RE.fullmatch(selected_folder):
            raise InvalidArgumentsError("invalid MAP folder path")
        bounded = max(1, min(int(limit), MAX_RECENT_QUERY_LIMIT))
        map_path = self.sessions.map_path

        def succeeded(messages: list[dict]) -> None:
            if self.dependencies.contacts is not None:
                for message in messages:
                    address = (
                        message.get("sender")
                        or message.get("sender_address")
                        or message.get("sender_phone_norm")
                    )
                    resolved = self.dependencies.contacts.resolve(address)
                    if resolved:
                        message["contact_name"] = resolved
            success(messages)

        self._queue(
            lambda: list_recent_messages(
                map_path, folder=selected_folder, limit=bounded
            ),
            succeeded,
            lambda error: self._operation_failed("Query", error, failure),
        )

    def sync_contacts(self, success: Success, failure: Failure) -> None:
        if self.sessions.pbap is None:
            raise NotReadyError(
                "PBAP session not open — check Sync Contacts on the iPhone"
            )
        if (
            self.dependencies.pull_contacts is None
            or self.dependencies.on_contacts_pulled is None
        ):
            raise NotReadyError("contact sync is unavailable")
        pull_contacts = self.dependencies.pull_contacts
        on_contacts_pulled = self.dependencies.on_contacts_pulled

        def succeeded(pulled: int) -> None:
            try:
                count = on_contacts_pulled(pulled)
                self.invalidate_conversations()
                success(count)
            except Exception as error:
                self._operation_failed("ContactSync", error, failure)

        self._queue(
            pull_contacts,
            succeeded,
            lambda error: self._operation_failed("ContactSync", error, failure),
        )

    def is_healthy(self) -> bool:
        return self.sessions.map is not None
