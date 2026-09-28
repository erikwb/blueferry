"""Session D-Bus transport adapter for backend application operations."""
from __future__ import annotations

import json
import logging

import dbus
import dbus.exceptions
import dbus.lowlevel
import dbus.service

from blueferry.backend_operations import BackendDependencies, BackendOperations, SessionState
from blueferry.background_worker import BackgroundWorker
from blueferry.bus import get_session_bus
from blueferry.client_activation import GTK_CLIENT
from blueferry.dbus_security import CallerGuard
from blueferry.errors import (
    BlueFerryError,
    InvalidArgumentsError,
    OperationFailedError,
    ResponseTooLargeError,
)
from blueferry.limits import MAX_DBUS_JSON_BYTES
from blueferry.protocol import (
    BUS_NAME,
    CALL_HISTORY_IFACE,
    CALLS_IFACE,
    ERROR_PREFIX,
    EVENTS_IFACE,
    MEDIA_IFACE,
    OBJECT_PATH,
    PRESENCE_IFACE,
    TETHER_IFACE,
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
        self._projection_worker = BackgroundWorker("blueferry-projection", maximum=1)
        # One active lookup plus an interactive request queued behind a
        # cancelled passive lookup. The executor still runs one job at a time.
        self._wallet_worker = BackgroundWorker("blueferry-wallet", maximum=2)

    @property
    def caller_guard(self) -> CallerGuard:
        """Shared so every media entry point draws from the same buckets."""
        return self._caller_guard

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

    def _conversation_call(self, sender, action, invoke, error_handler) -> None:
        self._async(
            lambda: self._authorized(sender, action, lambda: self.operations.prepare_conversations(
                self._projection_worker.submit,
                lambda: self._async(invoke, error_handler),
                lambda error: error_handler(self._dbus_error(error)),
            )),
            error_handler,
        )

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
        self._conversation_call(
            sender, "send",
            lambda: self.operations.send_to_thread(
                thread_key, body, bool(confirm_group), reply_handler,
                lambda error: error_handler(self._dbus_error(error)),
            ),
            error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="ssbs", out_signature="s",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def SendToThreadChecked(
        self, thread_key: str, body: str, confirm_group: bool,
        expected_group_token: str, reply_handler, error_handler, sender=None,
    ) -> None:
        self._conversation_call(
            sender, "send",
            lambda: self.operations.send_to_thread(
                thread_key, body, bool(confirm_group), reply_handler,
                lambda error: error_handler(self._dbus_error(error)),
                expected_group_token=expected_group_token,
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
        IFACE, in_signature="u", out_signature="s", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def ListThreads(self, limit: int, reply_handler, error_handler, sender=None) -> None:
        self._conversation_call(
            sender, "read", lambda: reply_handler(
                self._json_response(self.operations.list_threads(limit)),
            ), error_handler,
        )

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="u", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def MarkThreadRead(self, thread_key: str, reply_handler, error_handler, sender=None) -> None:
        def mark() -> None:
            updated = self.operations.mark_thread_read(thread_key)
            if updated:
                self.emit_history_changed()
            reply_handler(updated)

        self._conversation_call(sender, "read", mark, error_handler)

    @dbus.service.method(
        IFACE, in_signature="sb", out_signature="b", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def SetThreadStarred(
        self, thread_key: str, starred: bool, reply_handler, error_handler, sender=None,
    ) -> None:
        def update() -> None:
            result = self.operations.set_thread_starred(thread_key, bool(starred))
            self.emit_history_changed()
            reply_handler(result)

        self._conversation_call(sender, "settings", update, error_handler)

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
        IFACE, in_signature="s", out_signature="ay", sender_keyword="sender"
    )
    def GetContactPhoto(self, address: str, sender=None) -> dbus.ByteArray:
        return self._sync(lambda: self._authorized(
            sender, "contact-photo",
            lambda: dbus.ByteArray(self.operations.contact_photo(address)),
        ))

    @dbus.service.method(
        IFACE, in_signature="sas", out_signature="s", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def SetGroupParticipants(
        self, thread_key: str, recipients, reply_handler, error_handler, sender=None,
    ) -> None:
        def update() -> None:
            result = self._json_response(
                self.operations.set_group_participants(thread_key, recipients)
            )
            self.emit_history_changed()
            reply_handler(result)

        self._conversation_call(sender, "settings", update, error_handler)

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
        IFACE, in_signature="asb", out_signature="u", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def DeleteThreads(
        self, thread_keys, confirmed: bool, reply_handler, error_handler, sender=None,
    ) -> None:
        def delete() -> None:
            removed = self.operations.delete_threads(
                thread_keys, bool(confirmed)
            )
            self.emit_history_changed()
            reply_handler(removed)

        self._conversation_call(sender, "conversation-delete", delete, error_handler)

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
        IFACE, in_signature="", out_signature="b", sender_keyword="sender"
    )
    def GetContactsOnlyNotifications(self, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "status",
            self.operations.get_contacts_only_notifications,
        ))

    @dbus.service.method(
        IFACE, in_signature="b", out_signature="b", sender_keyword="sender"
    )
    def SetContactsOnlyNotifications(
        self, enabled: bool, sender=None,
    ) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.set_contacts_only_notifications(
                bool(enabled)
            ),
        ))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="b", sender_keyword="sender"
    )
    def GetAncsNotificationActions(self, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "status",
            self.operations.get_ancs_notification_actions,
        ))

    @dbus.service.method(
        IFACE, in_signature="b", out_signature="b", sender_keyword="sender"
    )
    def SetAncsNotificationActions(self, enabled: bool, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.set_ancs_notification_actions(bool(enabled)),
        ))

    @dbus.service.method(
        IFACE, in_signature="b", out_signature="b", sender_keyword="sender"
    )
    def SetPhoneBatteryWarning(self, enabled: bool, sender=None) -> bool:
        """Opt in or out of the low-battery desktop warning for the phone."""
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.set_phone_battery_warning(bool(enabled)),
        ))

    @dbus.service.method(
        PRESENCE_IFACE, in_signature="bu", out_signature="s", sender_keyword="sender"
    )
    def SetProximityLock(self, enabled: bool, grace_seconds: int, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(self.operations.set_proximity_lock(
                bool(enabled), int(grace_seconds)
            )),
        ))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetNotificationOpenMap(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "status",
            lambda: self._json_response(self.operations.get_notification_open_map()),
        ))

    @dbus.service.method(
        IFACE, in_signature="ss", out_signature="s", sender_keyword="sender"
    )
    def SetNotificationOpenTarget(
        self, bundle_id: str, target: str, sender=None,
    ) -> str:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(
                self.operations.set_notification_open_target(
                    str(bundle_id), str(target)
                )
            ),
        ))

    @dbus.service.method(
        IFACE, in_signature="ss", out_signature="b", sender_keyword="sender"
    )
    def OpenNotificationClick(
        self, click_id: str, activation_token: str, sender=None,
    ) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.open_notification_click(
                str(click_id), str(activation_token)
            ),
        ))

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="b", sender_keyword="sender"
    )
    def RemoveNotificationOpenTarget(self, bundle_id: str, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self.operations.remove_notification_open_target(str(bundle_id)),
        ))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetStoragePolicy(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "status", self.operations.get_storage_policy
        ))

    @dbus.service.method(
        IFACE, in_signature="s", out_signature="s", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def SetStoragePolicy(self, policy: str, reply_handler, error_handler, sender=None) -> None:
        self._storage_call(sender, "destructive", policy, reply_handler, error_handler)

    def _storage_call(self, sender, action, policy, reply_handler, error_handler) -> None:
        def changed(status) -> None:
            self.emit_history_changed()
            reply_handler(self._json_response(status))

        self._async(lambda: self._authorized(sender, action, lambda: (
            self.operations.change_storage_async(
                self._wallet_worker.submit, changed,
                lambda error: error_handler(self._dbus_error(error)), policy=policy,
            )
        )), error_handler)

    @dbus.service.method(
        IFACE, in_signature="", out_signature="s", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def UnlockStorage(self, reply_handler, error_handler, sender=None) -> None:
        self._storage_call(sender, "unlock", None, reply_handler, error_handler)

    def retry_storage_unlock(self, *, initialize: bool = False) -> None:
        """Daemon-only passive recovery, using the same worker as UI unlocks."""
        # A timed-out lookup may still be unwinding its native call. Reserve
        # the second slot for user action instead of queuing another poll.
        if self._wallet_worker.busy:
            return

        def changed(status) -> None:
            if status["storage_state"] == "ready":
                log.info("encrypted storage became available after keyring retry")
                self.emit_history_changed()

        self.operations.retry_storage_async(
            self._wallet_worker.submit, changed,
            lambda error: log.warning("background storage retry failed: %s", error),
            initialize=initialize,
        )

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
        CALL_HISTORY_IFACE, in_signature="u", out_signature="s", sender_keyword="sender"
    )
    def ListCallHistory(self, limit: int, sender=None) -> str:
        """Retained iPhone call history, newest first (opt-in feature)."""
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(self.operations.list_call_history(limit)),
        ))

    @dbus.service.method(
        CALL_HISTORY_IFACE, in_signature="", out_signature="u",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def SyncCallHistory(self, reply_handler, error_handler, sender=None) -> None:
        def respond(count: int) -> None:
            reply_handler(dbus.UInt32(count))

        self._async(
            lambda: self._authorized(
                sender,
                "call-history-sync",
                lambda: self.operations.sync_call_history(
                    respond,
                    lambda error: error_handler(self._dbus_error(error)),
                ),
            ),
            error_handler,
        )

    @dbus.service.method(
        CALL_HISTORY_IFACE, in_signature="bb", out_signature="s", sender_keyword="sender"
    )
    def SetCallHistory(
        self, enabled: bool, missed_call_notifications: bool, sender=None,
    ) -> str:
        """Opt in or out of call history; off erases the retained calls."""
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(self.operations.set_call_history(
                bool(enabled), bool(missed_call_notifications),
            )),
        ))

    @dbus.service.method(
        IFACE, in_signature="", out_signature="b", sender_keyword="sender"
    )
    def IsHealthy(self, sender=None) -> bool:
        return self._sync(lambda: self._authorized(
            sender, "status", self.operations.is_healthy
        ))

    @dbus.service.method(
        IFACE, in_signature="ss", out_signature="b", sender_keyword="sender",
    )
    def OpenLegacyGtkMessage(self, handle: str, application_owner: str, sender=None) -> bool:
        """Deliver to an older GTK process without broadcasting to other clients."""
        return self._sync(lambda: self._authorized(
            sender, "read", lambda: self._open_legacy_gtk_message(handle, application_owner),
        ))

    def _open_legacy_gtk_message(self, handle: str, application_owner: str) -> bool:
        if not handle or len(handle) > 1024 or len(application_owner) > 255:
            raise InvalidArgumentsError("invalid legacy client activation")
        try:
            dbus.validate_bus_name(application_owner, allow_well_known=False)
        except ValueError as error:
            raise InvalidArgumentsError("invalid legacy application owner") from error
        bus = self.connection
        broker = dbus.Interface(
            bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False),
            "org.freedesktop.DBus",
        )
        try:
            if bus.get_name_owner(GTK_CLIENT.desktop_id) != application_owner:
                return False
            pid = broker.GetConnectionUnixProcessID(application_owner)
        except dbus.DBusException:
            return False  # the selected window closed before delivery
        for peer in bus.list_names():
            if not peer.startswith(":"):
                continue
            try:
                if broker.GetConnectionUnixProcessID(peer) != pid:
                    continue
            except dbus.DBusException:
                continue  # a connection disappeared during the snapshot
            message = dbus.lowlevel.SignalMessage(OBJECT_PATH, EVENTS_IFACE, "OpenMessageRequested")
            message.set_destination(peer)
            message.append(handle, signature="s")
            bus.send_message(message)
        return True

    # ---- Media1: opt-in iPhone now-playing and media control -------------

    @dbus.service.method(
        MEDIA_IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetNowPlaying(self, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "media-read",
            lambda: self._json_response(self.operations.now_playing()),
        ))

    @dbus.service.method(
        MEDIA_IFACE, in_signature="b", out_signature="s", sender_keyword="sender"
    )
    def SetMediaControl(self, enabled: bool, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(
                self.operations.set_media_control(bool(enabled))
            ),
        ))

    @dbus.service.method(
        MEDIA_IFACE, in_signature="b", out_signature="s", sender_keyword="sender"
    )
    def SetMprisPlayer(self, enabled: bool, sender=None) -> str:
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(
                self.operations.set_media_mpris(bool(enabled))
            ),
        ))

    @dbus.service.method(
        MEDIA_IFACE, in_signature="s", out_signature="",
        async_callbacks=("reply_handler", "error_handler"),
        sender_keyword="sender",
    )
    def SendMediaCommand(
        self, command: str, reply_handler, error_handler, sender=None,
    ) -> None:
        self._async(
            lambda: self._authorized(
                sender, "media-command",
                lambda: self.operations.send_media_command(
                    str(command), reply_handler,
                    lambda error: error_handler(self._dbus_error(error)),
                ),
            ),
            error_handler,
        )

    # ---- Calls1: optional HFP call control --------------------------------

    def _call_control(self, sender, action, invoke, reply_handler, error_handler) -> None:
        """Authorize, then run one asynchronous call-control operation."""
        self._async(
            lambda: self._authorized(sender, action, lambda: invoke(
                reply_handler,
                lambda error: error_handler(self._dbus_error(error)),
            )),
            error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="b", out_signature="s", sender_keyword="sender"
    )
    def SetCallsEnabled(self, enabled: bool, sender=None) -> str:
        """Save the opt-in and apply it now; returns the calls_* status keys."""
        return self._sync(lambda: self._authorized(
            sender, "settings",
            lambda: self._json_response(self.operations.set_calls_enabled(bool(enabled))),
        ))

    @dbus.service.method(
        CALLS_IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def ListCalls(self, sender=None) -> str:
        """Unicast snapshot of current calls, including caller identities."""
        return self._sync(lambda: self._authorized(
            sender, "read",
            lambda: self._json_response(self.operations.list_calls()),
        ))

    @dbus.service.method(
        CALLS_IFACE, in_signature="s", out_signature="s", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def Dial(self, number: str, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-dial",
            lambda success, failure: self.operations.dial(
                str(number), lambda call_id: success(str(call_id or "")), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="s", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def Answer(self, call_id: str, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-answer",
            lambda success, failure: self.operations.answer_call(
                str(call_id), lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="s", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def Hangup(self, call_id: str, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-control",
            lambda success, failure: self.operations.hangup_call(
                str(call_id), lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def HangupAll(self, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-control",
            lambda success, failure: self.operations.hangup_all_calls(
                lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="ss", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def SendTones(
        self, call_id: str, tones: str, reply_handler, error_handler, sender=None,
    ) -> None:
        self._call_control(
            sender, "calls-control",
            lambda success, failure: self.operations.send_call_tones(
                str(call_id), str(tones), lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def SwapCalls(self, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-control",
            lambda success, failure: self.operations.swap_calls(
                lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )

    @dbus.service.method(
        CALLS_IFACE, in_signature="", out_signature="", sender_keyword="sender",
        async_callbacks=("reply_handler", "error_handler"),
    )
    def HoldAndAnswer(self, reply_handler, error_handler, sender=None) -> None:
        self._call_control(
            sender, "calls-answer",
            lambda success, failure: self.operations.hold_and_answer_call(
                lambda _result: success(), failure,
            ),
            reply_handler, error_handler,
        )
    # ---- Tether1: opt-in Bluetooth PAN through the iPhone hotspot --------

    @dbus.service.method(
        TETHER_IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def GetState(self, sender=None) -> str:
        """Tether state JSON: state, interface name, backend, error token."""
        return self._sync(lambda: self._authorized(
            sender, "status",
            lambda: self._json_response(self.operations.tether_state()),
        ))

    @dbus.service.method(
        TETHER_IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def Connect(self, sender=None) -> str:
        """Start tethering; the outcome arrives through TetherChanged."""
        return self._sync(lambda: self._authorized(
            sender, "tether",
            lambda: self._json_response(self.operations.tether_connect()),
        ))

    @dbus.service.method(
        TETHER_IFACE, in_signature="", out_signature="s", sender_keyword="sender"
    )
    def Disconnect(self, sender=None) -> str:
        """Stop tethering; only the PAN link, never the phone connection."""
        return self._sync(lambda: self._authorized(
            sender, "tether",
            lambda: self._json_response(self.operations.tether_disconnect()),
        ))

    @dbus.service.signal(TETHER_IFACE, signature="")
    def TetherChanged(self):
        """Tether state changed; clients fetch it with GetState."""

    def emit_tether_changed(self) -> None:
        try:
            self.TetherChanged()
        except Exception:
            log.exception("TetherChanged emit failed")

    @dbus.service.signal(EVENTS_IFACE, signature="a{sv}")
    def HistoryChanged(self, props):
        """Private history changed; payload contains only a local revision."""

    @dbus.service.signal(EVENTS_IFACE, signature="")
    def StatusChanged(self):
        """Backend status changed; clients fetch a private snapshot."""

    @dbus.service.signal(EVENTS_IFACE, signature="s")
    def OpenMessageRequested(self, handle: str):
        """A desktop notification requested an opaque message handle."""

    @dbus.service.signal(EVENTS_IFACE, signature="")
    def CallHistoryChanged(self):
        """Retained call history changed; clients call ListCallHistory."""

    @dbus.service.signal(EVENTS_IFACE, signature="")
    def NowPlayingChanged(self):
        """iPhone now-playing changed; clients call Media1.GetNowPlaying."""

    def emit_now_playing_changed(self) -> None:
        try:
            self.NowPlayingChanged()
        except Exception:
            log.exception("NowPlayingChanged emit failed")

    @dbus.service.signal(EVENTS_IFACE, signature="")
    def CallsChanged(self):
        """Call state changed; clients fetch details with Calls1.ListCalls."""

    def emit_calls_changed(self) -> None:
        try:
            self.CallsChanged()
        except Exception:
            log.exception("CallsChanged emit failed")

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

    def emit_call_history_changed(self) -> None:
        try:
            self.CallHistoryChanged()
        except Exception:
            log.exception("CallHistoryChanged emit failed")

    def emit_open_message(self, handle: str) -> None:
        try:
            self.OpenMessageRequested(str(handle)[:1024])
        except Exception:
            log.exception("OpenMessageRequested emit failed")

    def close(self) -> None:
        storage = self.operations.dependencies.storage
        if storage is not None:
            storage.cancel_pending()
        self._projection_worker.close()
        self._wallet_worker.close()
        self._caller_guard.close()


def claim_bus_name() -> dbus.service.BusName:
    """Acquire the backend's well-known session-bus name without queuing."""
    return dbus.service.BusName(
        BUS_NAME,
        bus=get_session_bus(),
        do_not_queue=True,
        replace_existing=False,
    )
