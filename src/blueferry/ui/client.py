"""DaemonClient — the UI's link to the running BlueFerry daemon.

The daemon owns `io.weirdware.BlueFerry` on the session bus. This client:
  • subscribes to content-free Events1 invalidations and re-emits them as
    GObject signals the UI pages connect to;
  • calls its messaging methods;
  • reads history and conversation routing through the daemon, so every UI
    uses the same correlation and recipient-safety rules.

Slow methods are issued asynchronously so the UI never blocks.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ClassVar

import dbus
import dbus.exceptions
import dbus.mainloop
from gi.repository import GLib, GObject

from blueferry.backend_lifecycle import ensure_backend_current
from blueferry.bus import get_session_bus
from blueferry.models import BackendStatus, Thread
from blueferry.pair_setup import configuration_status
from blueferry.protocol import (
    BUS_NAME,
    EVENTS_IFACE,
    MESSAGES_IFACE,
    OBEX_CALL_TIMEOUT_SEC,
    OBJECT_PATH,
)

log = logging.getLogger(__name__)


def _plain(value):
    """Recursively convert dbus-python types into plain Python values."""
    if isinstance(value, dbus.Dictionary):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, dbus.Array):
        return [_plain(v) for v in value]
    if isinstance(value, dbus.String):
        return str(value)
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(
        value,
        dbus.Int16 | dbus.Int32 | dbus.Int64 | dbus.UInt16 | dbus.UInt32 | dbus.UInt64 | dbus.Byte,
    ):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    return value


def dbus_error_text(e: Exception) -> str:
    if isinstance(e, dbus.exceptions.DBusException):
        return e.get_dbus_message() or e.get_dbus_name() or str(e)
    return str(e)


class DaemonClient(GObject.Object):
    """Live link to the daemon. Emits GObject signals as D-Bus signals arrive."""

    __gsignals__: ClassVar = {
        "history-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "status-invalidated": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "availability-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "open-message-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._bus = get_session_bus()
        self._matches: list = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blueferry-gtk-dbus")
        self._stopped = False
        self.available = False  # is the daemon reachable on D-Bus?
        self.healthy = False  # is the MAP session up?
        self._subscribe()
        if configuration_status()["configured"]:
            self.ensure_backend_current_async()

    # ---- signal subscription -------------------------------------------

    def _subscribe(self) -> None:
        # add_signal_receiver works even before the daemon is up — delivery
        # just starts once it claims the bus name.
        self._matches.append(
            self._bus.add_signal_receiver(
                lambda props: self.emit("history-changed", _plain(props)),
                dbus_interface=EVENTS_IFACE,
                signal_name="HistoryChanged",
                bus_name=BUS_NAME,
                path=OBJECT_PATH,
            )
        )
        self._matches.append(
            self._bus.add_signal_receiver(
                lambda handle: self.emit("open-message-requested", str(handle)),
                dbus_interface=EVENTS_IFACE,
                signal_name="OpenMessageRequested",
                bus_name=BUS_NAME,
                path=OBJECT_PATH,
            )
        )
        self._matches.append(
            self._bus.add_signal_receiver(
                lambda: self.emit("status-invalidated"),
                dbus_interface=EVENTS_IFACE,
                signal_name="StatusChanged",
                bus_name=BUS_NAME,
                path=OBJECT_PATH,
            )
        )

    def stop(self) -> None:
        self._stopped = True
        for m in self._matches:
            try:
                m.remove()
            except Exception:
                log.debug("could not remove backend signal watch", exc_info=True)
        self._matches = []
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ---- proxy helpers --------------------------------------------------

    def _iface(self, name: str) -> dbus.Interface:
        return dbus.Interface(self._bus.get_object(BUS_NAME, OBJECT_PATH), name)

    @staticmethod
    def _private_call(method: str, *args, timeout: int):
        """Make a blocking call on a worker-owned connection."""
        bus = dbus.SessionBus(
            private=True,
            mainloop=dbus.mainloop.NULL_MAIN_LOOP,
        )
        try:
            iface = dbus.Interface(bus.get_object(BUS_NAME, OBJECT_PATH), MESSAGES_IFACE)
            return getattr(iface, method)(*args, timeout=timeout)
        finally:
            bus.close()

    def _submit(self, operation, on_ok, on_err=None) -> Future:
        future = self._executor.submit(operation)

        def completed(result: Future) -> None:
            if self._stopped:
                return
            try:
                value = result.result()
            except Exception as error:
                if on_err is not None:
                    message = dbus_error_text(error)

                    def deliver_error() -> bool:
                        on_err(message)
                        return False

                    GLib.idle_add(deliver_error)
                return

            def deliver_value() -> bool:
                on_ok(value)
                return False

            GLib.idle_add(deliver_value)

        future.add_done_callback(completed)
        return future

    def _set_availability(self, reachable: bool, healthy: bool) -> bool:
        self.healthy = healthy
        if reachable != self.available:
            self.available = reachable
            self.emit("availability-changed", reachable)
        return False

    def record_status(self, status: BackendStatus) -> None:
        """Update availability from an already-fetched status snapshot."""
        self._set_availability(
            status.daemon,
            status.map,
        )

    def ensure_backend_current_async(self) -> None:
        def operation() -> Mapping:
            def private_status() -> dict:
                raw = str(self._private_call("GetStatus", timeout=20))
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("backend returned an invalid status response")
                return value

            return ensure_backend_current(status_reader=private_status)

        def ready(status: Mapping) -> bool:
            current = BackendStatus.from_dict(status)
            self.record_status(current)
            return False

        def failed(message: str) -> bool:
            log.warning("could not activate current backend: %s", message)
            self._set_availability(False, False)
            return False

        self._submit(operation, ready, failed)

    def refresh_availability_async(self, on_done=None) -> None:
        def operation() -> bool:
            return bool(self._private_call("IsHealthy", timeout=5))

        def ready(healthy: bool) -> bool:
            self._set_availability(True, healthy)
            if on_done is not None:
                on_done(True)
            return False

        def failed(_message: str) -> bool:
            self._set_availability(False, False)
            if on_done is not None:
                on_done(False)
            return False

        self._submit(operation, ready, failed)

    # ---- Messages1 ------------------------------------------------------

    def send_message(self, recipient: str, body: str, on_ok, on_err) -> None:
        """Send asynchronously. on_ok(transfer_path) / on_err(text)."""
        try:
            self._iface(MESSAGES_IFACE).Send(
                recipient,
                body,
                timeout=OBEX_CALL_TIMEOUT_SEC,
                reply_handler=lambda t: on_ok(str(t)),
                error_handler=lambda e: on_err(dbus_error_text(e)),
            )
        except dbus.exceptions.DBusException as e:
            on_err(dbus_error_text(e))

    def send_to_thread(
        self,
        thread_key: str,
        body: str,
        *,
        confirm_group: bool,
        on_ok,
        on_err,
    ) -> None:
        try:
            self._iface(MESSAGES_IFACE).SendToThread(
                thread_key,
                body,
                dbus.Boolean(confirm_group),
                timeout=OBEX_CALL_TIMEOUT_SEC,
                reply_handler=lambda transfer: on_ok(str(transfer)),
                error_handler=lambda error: on_err(dbus_error_text(error)),
            )
        except dbus.exceptions.DBusException as error:
            on_err(dbus_error_text(error))

    def sync_contacts(self, on_ok, on_err) -> None:
        try:
            self._iface(MESSAGES_IFACE).SyncContacts(
                timeout=OBEX_CALL_TIMEOUT_SEC,
                reply_handler=lambda count: on_ok(int(count)),
                error_handler=lambda error: on_err(dbus_error_text(error)),
            )
        except dbus.exceptions.DBusException as error:
            on_err(dbus_error_text(error))

    # ---- backend-owned history -----------------------------------------

    def list_threads_async(self, on_ok, on_err=None, limit: int = 1000) -> None:
        def operation() -> list[Thread]:
            raw = str(
                self._private_call(
                    "ListThreads",
                    dbus.UInt32(limit),
                    timeout=20,
                )
            )
            value = json.loads(raw)
            return (
                [Thread.from_dict(item) for item in value if isinstance(item, Mapping)]
                if isinstance(value, list)
                else []
            )

        self._submit(operation, on_ok, on_err)

    def find_contacts_async(self, query: str, on_ok, on_err=None) -> None:
        """Find cached message destinations without blocking the GTK thread."""
        selected = query.strip()
        if not selected:
            on_ok([])
            return

        def operation() -> list[tuple[str, str]]:
            raw = str(
                self._private_call(
                    "FindContacts",
                    selected,
                    timeout=8,
                )
            )
            value = json.loads(raw)
            if not isinstance(value, list):
                return []
            return [
                (str(item["name"]), str(item["address"]))
                for item in value
                if isinstance(item, Mapping)
                and "name" in item
                and "address" in item
            ]

        self._submit(operation, on_ok, on_err)

    def get_status_async(self, on_ok, on_err=None) -> None:
        def operation() -> BackendStatus:
            raw = str(self._private_call("GetStatus", timeout=10))
            value = json.loads(raw)
            return BackendStatus.from_dict(value) if isinstance(value, dict) else BackendStatus()

        self._submit(operation, on_ok, on_err)

    def clear_history_async(self, on_ok, on_err) -> None:
        self._submit(
            lambda: self._private_call("ClearHistory", dbus.Boolean(True), timeout=20),
            lambda _value: on_ok(),
            on_err,
        )

    def set_notification_policy_async(self, policy: str, on_ok, on_err) -> None:
        self._submit(
            lambda: str(self._private_call("SetNotificationPolicy", policy, timeout=10)),
            on_ok,
            on_err,
        )

    def set_storage_policy_async(self, policy: str, on_ok, on_err) -> None:
        def operation() -> dict:
            raw = str(self._private_call("SetStoragePolicy", policy, timeout=30))
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}

        self._submit(operation, on_ok, on_err)

    def unlock_storage_async(self, on_ok, on_err) -> None:
        def operation() -> dict:
            raw = str(self._private_call("UnlockStorage", timeout=30))
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}

        self._submit(operation, on_ok, on_err)
