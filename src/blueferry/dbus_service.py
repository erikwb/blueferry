"""Session D-Bus transport adapter for backend application operations."""
from __future__ import annotations

import json
import logging

import dbus
import dbus.exceptions
import dbus.service

from blueferry.backend_operations import BackendDependencies, BackendOperations, SessionState
from blueferry.bus import get_session_bus
from blueferry.dbus_security import CallerGuard
from blueferry.errors import BlueFerryError, OperationFailedError, ResponseTooLargeError
from blueferry.limits import MAX_DBUS_JSON_BYTES
from blueferry.protocol import (
    BUS_NAME,
    ERROR_PREFIX,
    EVENTS_IFACE,
    OBJECT_PATH,
)
from blueferry.protocol import (
    MESSAGES_IFACE as IFACE,
)

log = logging.getLogger(__name__)


class MessagesService(dbus.service.Object):
    """Translate stable Messages1 wire types to :class:`BackendOperations`."""

    def __init__(
        self,
        bus_name: dbus.service.BusName,
        sessions: SessionState,
        dependencies: BackendDependencies | None = None,
        operations: BackendOperations | None = None,
        caller_guard: CallerGuard | None = None,
    ) -> None:
        super().__init__(bus_name, OBJECT_PATH)
        self._caller_guard = caller_guard or CallerGuard(bus_name.get_bus())
        self._change_revision = 0
        self.operations = operations or BackendOperations(sessions, dependencies)

    @staticmethod
    def _dbus_error(error: Exception) -> dbus.exceptions.DBusException:
        suffix = (
            error.dbus_suffix
            if isinstance(error, BlueFerryError)
            else "Failed"
        )
        if isinstance(error, OperationFailedError):
            message = f"{error.operation} failed; check the daemon log for details"
        elif isinstance(error, BlueFerryError):
            message = str(error) or "operation failed"
        else:
            message = "internal backend error; check the daemon log for details"
        return dbus.exceptions.DBusException(message[:1024], name=f"{ERROR_PREFIX}.{suffix}")

    @staticmethod
    def _json_response(value) -> str:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DBUS_JSON_BYTES:
            raise ResponseTooLargeError(
                "result is too large; request fewer records"
            )
        return encoded

    def _authorized(self, sender: str | None, action: str, invoke):
        self._caller_guard.authorize(sender, action)
        return invoke()

    def _async(self, invoke, error_handler) -> None:
        try:
            invoke()
        except Exception as error:
            if not isinstance(error, BlueFerryError):
                log.exception("unexpected asynchronous D-Bus operation failure")
            error_handler(self._dbus_error(error))

    def _sync(self, invoke):
        try:
            return invoke()
        except Exception as error:
            if not isinstance(error, BlueFerryError):
                log.exception("unexpected synchronous D-Bus operation failure")
            raise self._dbus_error(error) from error

    @dbus.service.method(
        IFACE, in_signature="ss", out_signature="s",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def Send(
        self, recipient: str, body: str, reply_handler, error_handler,
        sender=None,
    ) -> None:
        def send() -> None:
            log.info("DBus Send called (%d-byte body)", len(body.encode("utf-8")))
            self.operations.send(
                recipient, body, reply_handler,
                lambda error: error_handler(self._dbus_error(error)),
            )

        self._async(
            lambda: self._authorized(sender, "send", send),
            error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="ssb", out_signature="s",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def SendToThread(
        self, thread_key: str, body: str, confirm_group: bool,
        reply_handler, error_handler, sender=None,
    ) -> None:
        self._async(
            lambda: self._authorized(
                sender,
                "send",
                lambda: self.operations.send_to_thread(
                    thread_key, body, bool(confirm_group), reply_handler,
                    lambda error: error_handler(self._dbus_error(error)),
                ),
            ),
            error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="asu", out_signature="s", sender_keyword="sender"
    )
    def ListEvents(self, kinds, limit: int, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(self.operations.list_events(kinds, limit)),
        ))

    @dbus.service.method(
        IFACE, in_signature="u", out_signature="s", sender_keyword="sender"
    )
    def ListThreads(self, limit: int, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(self.operations.list_threads(limit)),
        ))

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="s", sender_keyword="sender"
    )
    def FindContacts(self, query: str, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(self.operations.find_contacts(query)),
        ))

    @dbus.service.method(
        IFACE, in_signature="uu", out_signature="s", sender_keyword="sender"
    )
    def ListContacts(self, offset: int, limit: int, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(
                self.operations.list_contacts(offset, limit)
            ),
        ))

    @dbus.service.method(
        IFACE, in_signature="sas", out_signature="s", sender_keyword="sender"
    )
    def SetGroupParticipants(self, thread_key: str, recipients, sender=None) -> str:
        def update() -> str:
            result = self._json_response(
                self.operations.set_group_participants(thread_key, recipients)
            )
            self.emit_history_changed()
            return result

        return self._sync(lambda: self._authorized(sender, "settings", update))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetStatus(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "status",
            lambda: self._json_response(self.operations.status()),
        ))

    @dbus.service.method(
        IFACE, in_signature="b", out_signature="", sender_keyword="sender"
    )
    def ClearHistory(self, confirmed: bool, sender=None) -> None:
        def clear() -> None:
            self.operations.clear_history(bool(confirmed))
            self.emit_history_changed()

        self._sync(lambda: self._authorized(sender, "destructive", clear))

    @dbus.service.method(
        IFACE, in_signature="asb", out_signature="u", sender_keyword="sender"
    )
    def DeleteThreads(self, thread_keys, confirmed: bool, sender=None) -> int:
        def delete() -> int:
            removed = self.operations.delete_threads(
                thread_keys, bool(confirmed)
            )
            self.emit_history_changed()
            return removed

        return self._sync(
            lambda: self._authorized(sender, "destructive", delete)
        )

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetNotificationPolicy(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "status", self.operations.get_notification_policy
        ))

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="s", sender_keyword="sender"
    )
    def SetNotificationPolicy(self, policy: str, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.set_notification_policy(policy),
        ))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetStoragePolicy(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "status", self.operations.get_storage_policy
        ))

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="s", sender_keyword="sender"
    )
    def SetStoragePolicy(self, policy: str, sender=None) -> str:
        def change() -> str:
            result = self._json_response(self.operations.set_storage_policy(policy))
            self.emit_history_changed()
            return result

        return self._sync(lambda: self._authorized(sender, "destructive", change))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def UnlockStorage(self, sender=None) -> str:
        def unlock() -> str:
            result = self._json_response(self.operations.unlock_storage())
            self.emit_history_changed()
            return result

        return self._sync(lambda: self._authorized(sender, "unlock", unlock))

    @dbus.service.method(
        IFACE, in_signature="su", out_signature="s",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def ListRecent(
        self, folder: str, limit: int, reply_handler, error_handler, sender=None
    ) -> None:
        def respond(messages) -> None:
            try:
                reply_handler(self._json_response(messages))
            except Exception as error:
                if not isinstance(error, BlueFerryError):
                    log.exception("unexpected D-Bus response serialization failure")
                error_handler(self._dbus_error(error))

        self._async(
            lambda: self._authorized(
                sender,
                "read",
                lambda: self.operations.list_recent(
                    folder, limit, respond,
                    lambda error: error_handler(self._dbus_error(error)),
                ),
            ),
            error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="", out_signature="u",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def SyncContacts(self, reply_handler, error_handler, sender=None) -> None:
        def respond(count: int) -> None:
            reply_handler(dbus.UInt32(count))

        self._async(
            lambda: self._authorized(
                sender,
                "contact-sync",
                lambda: self.operations.sync_contacts(
                    respond,
                    lambda error: error_handler(self._dbus_error(error)),
                ),
            ),
            error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="", out_signature="b", sender_keyword="sender"
    )
    def IsHealthy(self, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "status", self.operations.is_healthy
        ))

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def HistoryChanged(self, props):
        """Private history changed; payload contains only a local revision."""

    @dbus.service.signal(EVENTS_IFACE, signature="")
    def StatusChanged(self):
        """Backend status changed; clients fetch a private snapshot."""

    @dbus.service.signal(EVENTS_IFACE, signature="s")
    def OpenMessageRequested(self, handle: str):
        """A desktop notification requested an opaque message handle."""

    def emit_history_changed(self) -> None:
        try:
            self._change_revision += 1
            self.HistoryChanged(dbus.Dictionary(
                {"revision": dbus.UInt64(self._change_revision)}, signature="sv"
            ))
        except Exception:
            log.exception("history invalidation signal emit failed")

    def emit_status(self) -> None:
        try:
            self.StatusChanged()
        except Exception:
            log.exception("StatusChanged emit failed")

    def emit_open_message(self, handle: str) -> None:
        try:
            self.OpenMessageRequested(str(handle)[:1024])
        except Exception:
            log.exception("OpenMessageRequested emit failed")

    def close(self) -> None:
        self._caller_guard.close()


def claim_bus_name() -> dbus.service.BusName:
    """Acquire the backend's well-known session-bus name without queuing."""
    return dbus.service.BusName(
        BUS_NAME,
        bus=get_session_bus(),
        do_not_queue=True,
        replace_existing=False,
    )
