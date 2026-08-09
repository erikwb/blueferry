"""Toolkit-neutral synchronous client for the BlueFerry daemon."""
from __future__ import annotations

import json

import dbus
import dbus.exceptions

from blueferry.bus import get_session_bus
from blueferry.errors import BlueFerryError
from blueferry.models import BackendStatus, EventRecord, Thread
from blueferry.protocol import (
    BUS_NAME,
    MESSAGES_IFACE,
    OBEX_CALL_TIMEOUT_SEC,
    OBJECT_PATH,
)


class BackendError(BlueFerryError):
    pass


class BackendClient:
    def _iface(self, name: str) -> dbus.Interface:
        bus = get_session_bus()
        return dbus.Interface(bus.get_object(BUS_NAME, OBJECT_PATH), name)

    @staticmethod
    def _json(value, expected_type):
        parsed = json.loads(str(value))
        if not isinstance(parsed, expected_type):
            raise ValueError(f"backend returned {type(parsed).__name__}, expected "
                             f"{expected_type.__name__}")
        return parsed

    def status(self) -> BackendStatus:
        try:
            value = self._json(
                self._iface(MESSAGES_IFACE).GetStatus(timeout=8), dict,
            )
            return BackendStatus.from_dict(value)
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def threads(self, limit: int = 1000) -> list[Thread]:
        try:
            values = self._json(self._iface(MESSAGES_IFACE).ListThreads(
                dbus.UInt32(limit), timeout=15,
            ), list)
            return [Thread.from_dict(value) for value in values
                    if isinstance(value, dict)]
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def events(self, kinds: list[str], limit: int = 1000) -> list[EventRecord]:
        try:
            values = self._json(self._iface(MESSAGES_IFACE).ListEvents(
                dbus.Array(kinds, signature="s"), dbus.UInt32(limit), timeout=15,
            ), list)
            return [EventRecord.from_dict(value) for value in values
                    if isinstance(value, dict)]
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def find_contacts(self, query: str) -> list[tuple[str, str]]:
        try:
            values = self._json(self._iface(MESSAGES_IFACE).FindContacts(
                query, timeout=8
            ), list)
            return [
                (str(value["name"]), str(value["address"]))
                for value in values
                if isinstance(value, dict)
                and "name" in value and "address" in value
            ]
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
            return self._json(self._iface(MESSAGES_IFACE).ListRecent(
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
                dbus.Boolean(True), timeout=15
            )
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def notification_policy(self) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).GetNotificationPolicy(timeout=8))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_notification_policy(self, policy: str) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).SetNotificationPolicy(
                policy, timeout=8
            ))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def storage_policy(self) -> str:
        try:
            return str(self._iface(MESSAGES_IFACE).GetStoragePolicy(timeout=8))
        except dbus.exceptions.DBusException as error:
            raise BackendError(error.get_dbus_message() or str(error)) from error

    def set_storage_policy(self, policy: str) -> dict:
        try:
            return self._json(self._iface(MESSAGES_IFACE).SetStoragePolicy(
                policy, timeout=30
            ), dict)
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error

    def unlock_storage(self) -> dict:
        try:
            return self._json(
                self._iface(MESSAGES_IFACE).UnlockStorage(timeout=30), dict
            )
        except (dbus.exceptions.DBusException, ValueError) as error:
            raise BackendError(str(error)) from error
