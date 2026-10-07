"""Toolkit-neutral synchronous client for the BlueFerry daemon."""
from __future__ import annotations

from collections.abc import Callable

import dbus
import dbus.exceptions

from blueferry.bus import get_session_bus
from blueferry.client_wire import (
    decode_call_history,
    decode_contact_records,
    decode_contacts,
    decode_events,
    decode_json,
    decode_mapping,
    decode_thread,
    decode_threads,
)
from blueferry.errors import BlueFerryError
from blueferry.limits import MAX_CONTACT_PAGE
from blueferry.models import BackendStatus, CallHistoryEntry, EventRecord, Thread
from blueferry.protocol import (
    BUS_NAME,
    CALL_HISTORY_IFACE,
    CLEAR_CALL_TIMEOUT_SEC,
    CONTACT_CALL_TIMEOUT_SEC,
    DELETE_CALL_TIMEOUT_SEC,
    GROUP_ROUTE_CALL_TIMEOUT_SEC,
    MESSAGES_IFACE,
    OBEX_CALL_TIMEOUT_SEC,
    OBJECT_PATH,
    POLICY_CALL_TIMEOUT_SEC,
    PRESENCE_IFACE,
    SNAPSHOT_CALL_TIMEOUT_SEC,
    STATUS_CALL_TIMEOUT_SEC,
    STORAGE_CALL_TIMEOUT_SEC,
    backend_compatibility_error,
)


class BackendError(BlueFerryError):
    pass


class CompatibilityCache:
    """Daemon unique bus names already verified as API-compatible.

    A bus never reuses a unique name, so a replacement daemon is always a new
    entry and is checked again. Share one cache between clients that talk to
    the same session bus to skip the per-call GetStatus round trip.
    """

    def __init__(self) -> None:
        self._owners: set[str] = set()

    def __contains__(self, owner: object) -> bool:
        return owner in self._owners

    def add(self, owner: str) -> None:
        self._owners.add(owner)


def _dbus_message(error: Exception) -> str:
    if isinstance(error, dbus.exceptions.DBusException):
        return error.get_dbus_message() or str(error)
    return str(error)


def _unique_owner(interface: object) -> str | None:
    # dbus-python binds a proxy to the name's unique owner when it is created.
    # Test doubles and other factories without one are checked on every call.
    owner = getattr(interface, "bus_name", None)
    return owner if isinstance(owner, str) and owner.startswith(":") else None


class BackendClient:
    def __init__(
        self,
        *,
        interface_factory: Callable[[str], dbus.Interface] | None = None,
        compatibility: CompatibilityCache | None = None,
    ) -> None:
        self._interface_factory = interface_factory
        self._compatibility = compatibility if compatibility is not None else CompatibilityCache()

    def _raw_iface(self, name: str) -> dbus.Interface:
        if self._interface_factory is not None:
            return self._interface_factory(name)
        bus = get_session_bus()
        return dbus.Interface(bus.get_object(BUS_NAME, OBJECT_PATH), name)

    def _iface(self, name: str) -> dbus.Interface:
        interface = self._raw_iface(name)
        # Check the same owner-bound proxy used for the operation. A daemon
        # replacement has a new unique name, so it never inherits an earlier
        # process's compatibility.
        owner = _unique_owner(interface)
        if owner is not None and owner in self._compatibility:
            return interface
        try:
            status = decode_mapping(interface.GetStatus(timeout=STATUS_CALL_TIMEOUT_SEC))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error
        if error_message := backend_compatibility_error(status):
            raise BackendError(error_message)
        if owner is not None:
            self._compatibility.add(owner)
        return interface

    def is_healthy(self) -> bool:
        try:
            return bool(self._iface(MESSAGES_IFACE).IsHealthy(timeout=5))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def status(self, *, check_compatibility: bool = True) -> BackendStatus:
        """Read status; lifecycle recovery may inspect an older daemon first."""
        try:
            status = decode_mapping(
                self._raw_iface(MESSAGES_IFACE).GetStatus(
                    timeout=STATUS_CALL_TIMEOUT_SEC
                )
            )
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error
        if check_compatibility and (error_message := backend_compatibility_error(status)):
            raise BackendError(error_message)
        return BackendStatus.from_dict(status)

    def threads(self, limit: int = 1000) -> list[Thread]:
        try:
            return decode_threads(self._iface(MESSAGES_IFACE).ListThreads(
                dbus.UInt32(limit), timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def mark_thread_read(self, thread_key: str) -> int:
        try:
            return int(self._iface(MESSAGES_IFACE).MarkThreadRead(
                thread_key, timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_thread_starred(self, thread_key: str, starred: bool) -> bool:
        try:
            return bool(self._iface(MESSAGES_IFACE).SetThreadStarred(
                thread_key, dbus.Boolean(starred),
                timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def events(self, kinds: list[str], limit: int = 1000) -> list[EventRecord]:
        try:
            return decode_events(self._iface(MESSAGES_IFACE).ListEvents(
                dbus.Array(kinds, signature="s"), dbus.UInt32(limit),
                timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def find_contacts(self, query: str) -> list[tuple[str, str]]:
        try:
            return decode_contacts(self._iface(MESSAGES_IFACE).FindContacts(
                query, timeout=CONTACT_CALL_TIMEOUT_SEC
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def list_contacts(
        self, offset: int = 0, limit: int = MAX_CONTACT_PAGE
    ) -> list[tuple[str, list[str], list[str]]]:
        """One page of the cached phonebook, addresses grouped per person."""
        try:
            return decode_contact_records(self._iface(MESSAGES_IFACE).ListContacts(
                dbus.UInt32(offset), dbus.UInt32(limit),
                timeout=CONTACT_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def set_group_participants(
        self, thread_key: str, recipients: list[str]
    ) -> Thread:
        try:
            return decode_thread(
                self._iface(MESSAGES_IFACE).SetGroupParticipants(
                    thread_key,
                    dbus.Array(recipients, signature="s"),
                    timeout=GROUP_ROUTE_CALL_TIMEOUT_SEC,
                )
            )
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def send_to_thread(
        self, key: str, body: str, *, confirm_group: bool = False,
        expected_group_token: str = "",
    ) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).SendToThreadChecked(
                key, body, dbus.Boolean(confirm_group), expected_group_token,
                timeout=OBEX_CALL_TIMEOUT_SEC,
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def send(self, recipient: str, body: str) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).Send(
                recipient, body, timeout=OBEX_CALL_TIMEOUT_SEC,
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def recent(self, folder: str, limit: int) -> list[dict]:
        try:
            return decode_json(self._iface(MESSAGES_IFACE).ListRecent(
                folder, dbus.UInt32(limit), timeout=OBEX_CALL_TIMEOUT_SEC,
            ), list)
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def sync_contacts(self) -> int:
        try:
            return int(self._iface(MESSAGES_IFACE).SyncContacts(
                timeout=OBEX_CALL_TIMEOUT_SEC
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def _call_history_iface(self) -> dbus.Interface:
        # CallHistory1 has no GetStatus; check compatibility through
        # Messages1 and address CallHistory1 on that same owner-bound object.
        messages = self._iface(MESSAGES_IFACE)
        proxy = getattr(messages, "proxy_object", None)
        if proxy is None:
            return self._raw_iface(CALL_HISTORY_IFACE)
        return dbus.Interface(proxy, CALL_HISTORY_IFACE)

    def call_history(self, limit: int = 200) -> list[CallHistoryEntry]:
        try:
            return decode_call_history(self._call_history_iface().ListCallHistory(
                dbus.UInt32(limit), timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(_dbus_message(error)) from error

    def sync_call_history(self) -> int:
        try:
            return int(self._call_history_iface().SyncCallHistory(
                timeout=OBEX_CALL_TIMEOUT_SEC
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_call_history(self, enabled: bool, missed_call_notifications: bool) -> dict:
        try:
            return decode_mapping(self._call_history_iface().SetCallHistory(
                dbus.Boolean(enabled),
                dbus.Boolean(missed_call_notifications),
                timeout=POLICY_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(_dbus_message(error)) from error

    def clear_history(self) -> None:
        try:
            self._iface(MESSAGES_IFACE).ClearHistory(
                dbus.Boolean(True), timeout=CLEAR_CALL_TIMEOUT_SEC
            )
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def delete_threads(self, thread_keys: list[str]) -> int:
        try:
            return int(self._iface(MESSAGES_IFACE).DeleteThreads(
                dbus.Array(thread_keys, signature="s"),
                dbus.Boolean(True),
                timeout=DELETE_CALL_TIMEOUT_SEC,
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def notification_policy(self) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).GetNotificationPolicy(
                timeout=POLICY_CALL_TIMEOUT_SEC
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_notification_policy(self, policy: str) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).SetNotificationPolicy(
                policy, timeout=POLICY_CALL_TIMEOUT_SEC
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def contacts_only_notifications(self) -> bool:
        try:
            return bool(
                self._iface(MESSAGES_IFACE).GetContactsOnlyNotifications(
                    timeout=POLICY_CALL_TIMEOUT_SEC
                )
            )
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_contacts_only_notifications(self, enabled: bool) -> bool:
        try:
            return bool(
                self._iface(MESSAGES_IFACE).SetContactsOnlyNotifications(
                    dbus.Boolean(enabled), timeout=POLICY_CALL_TIMEOUT_SEC
                )
            )
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def _presence_iface(self) -> dbus.Interface:
        # Presence1 has no GetStatus; check compatibility through Messages1
        # and address Presence1 on that same owner-bound object.
        messages = self._iface(MESSAGES_IFACE)
        proxy = getattr(messages, "proxy_object", None)
        if proxy is None:
            return self._raw_iface(PRESENCE_IFACE)
        return dbus.Interface(proxy, PRESENCE_IFACE)

    def set_proximity_lock(self, enabled: bool, grace_seconds: int) -> dict:
        try:
            return decode_mapping(self._presence_iface().SetProximityLock(
                dbus.Boolean(enabled),
                dbus.UInt32(grace_seconds),
                timeout=POLICY_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(
                error.get_dbus_message()
                if isinstance(error, dbus.exceptions.DBusException)
                else str(error)
            ) from error

    def storage_policy(self) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).GetStoragePolicy(
                timeout=POLICY_CALL_TIMEOUT_SEC
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_storage_policy(self, policy: str) -> dict:
        try:
            return decode_mapping(self._iface(MESSAGES_IFACE).SetStoragePolicy(
                policy, timeout=STORAGE_CALL_TIMEOUT_SEC
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def unlock_storage(self) -> dict:
        try:
            return decode_mapping(
                self._iface(MESSAGES_IFACE).UnlockStorage(
                    timeout=STORAGE_CALL_TIMEOUT_SEC
                )
            )
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error
