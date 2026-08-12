"""Toolkit-neutral synchronous client for the BlueFerry daemon."""
from __future__ import annotations

import dbus
import dbus.exceptions

from blueferry.bus import get_session_bus
from blueferry.client_wire import (
    decode_contacts,
    decode_events,
    decode_json,
    decode_mapping,
    decode_status,
    decode_thread,
    decode_threads,
)
from blueferry.errors import BlueFerryError
from blueferry.models import BackendStatus, EventRecord, Thread
from blueferry.protocol import (
    BUS_NAME,
    CLEAR_CALL_TIMEOUT_SEC,
    CONTACT_CALL_TIMEOUT_SEC,
    GROUP_ROUTE_CALL_TIMEOUT_SEC,
    MESSAGES_IFACE,
    OBEX_CALL_TIMEOUT_SEC,
    OBJECT_PATH,
    POLICY_CALL_TIMEOUT_SEC,
    SNAPSHOT_CALL_TIMEOUT_SEC,
    STATUS_CALL_TIMEOUT_SEC,
    STORAGE_CALL_TIMEOUT_SEC,
)


class BackendError(BlueFerryError):
    pass


class BackendClient:
    def _iface(self, name: str) -> dbus.Interface:
        bus = get_session_bus()
        return dbus.Interface(bus.get_object(BUS_NAME, OBJECT_PATH), name)

    def status(self) -> BackendStatus:
        try:
            return decode_status(
                self._iface(MESSAGES_IFACE).GetStatus(
                    timeout=STATUS_CALL_TIMEOUT_SEC
                )
            )
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def threads(self, limit: int = 1000) -> list[Thread]:
        try:
            return decode_threads(self._iface(MESSAGES_IFACE).ListThreads(
                dbus.UInt32(limit), timeout=SNAPSHOT_CALL_TIMEOUT_SEC,
            ))
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

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
    ) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).SendToThread(
                key, body, dbus.Boolean(confirm_group),
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

    def clear_history(self) -> None:
        try:
            self._iface(MESSAGES_IFACE).ClearHistory(
                dbus.Boolean(True), timeout=CLEAR_CALL_TIMEOUT_SEC
            )
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
