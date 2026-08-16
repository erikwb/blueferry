"""Private stdin bridge from Quickshell to the session D-Bus service.

Quickshell does not provide a generic D-Bus QML type.  Keeping one helper
alive lets the shell use BlueFerry's authenticated D-Bus API without placing
message content, destinations, or contact queries in child-process argv.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from blueferry.bus import get_session_bus
from blueferry.client import BackendClient
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH

MAX_REQUEST_CHARS = 1_048_576
MAX_PENDING_REQUESTS = 32
REQUEST_WORKERS = 4


class RequestError(ValueError):
    """The local QML client sent a malformed bridge request."""


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestError("args must be an object")
    return value


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise RequestError(f"{name} must be a string")
    return value


def _texts(args: Mapping[str, Any], name: str) -> list[str]:
    value = args.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError(f"{name} must be a string array")
    return value


class QuickshellBridge:
    """Decode local requests and dispatch them through ``BackendClient``."""

    def __init__(
        self,
        client: BackendClient,
        output: TextIO = sys.stdout,
    ) -> None:
        self.client = client
        self.output = output
        self._output_lock = threading.Lock()

    def emit(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._output_lock:
            self.output.write(line)
            self.output.write("\n")
            self.output.flush()

    def emit_event(self, name: str, data: object = None) -> None:
        self.emit({"event": name, "data": data})

    def dispatch(self, method: str, args: Mapping[str, Any]) -> object:
        if method == "status":
            return self.client.status().to_dict()
        if method == "threads":
            limit = args.get("limit", 200)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise RequestError("limit must be an integer")
            return [thread.to_dict() for thread in self.client.threads(limit)]
        if method == "contacts":
            return [
                {"name": name, "address": address}
                for name, address in self.client.find_contacts(_text(args, "query"))
            ]
        if method == "send":
            return self.client.send(_text(args, "recipient"), _text(args, "body"))
        if method == "send_to_thread":
            confirm = args.get("confirm_group", False)
            if not isinstance(confirm, bool):
                raise RequestError("confirm_group must be a boolean")
            return self.client.send_to_thread(
                _text(args, "thread_key"),
                _text(args, "body"),
                confirm_group=confirm,
            )
        if method == "set_group_participants":
            return self.client.set_group_participants(
                _text(args, "thread_key"), _texts(args, "recipients")
            ).to_dict()
        if method == "set_notification_policy":
            return self.client.set_notification_policy(_text(args, "policy"))
        if method == "set_storage_policy":
            return self.client.set_storage_policy(_text(args, "policy"))
        if method == "unlock_storage":
            return self.client.unlock_storage()
        raise RequestError(f"unsupported method: {method}")

    def handle_line(self, line: str) -> None:
        request_id: object = None
        method: object = ""
        try:
            if len(line) > MAX_REQUEST_CHARS:
                raise RequestError("request is too large")
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise RequestError("request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if isinstance(request_id, bool) or not isinstance(request_id, int):
                raise RequestError("id must be an integer")
            if not isinstance(method, str) or not method:
                raise RequestError("method must be a non-empty string")
            result = self.dispatch(method, _mapping(request.get("args", {})))
            self.emit({"id": request_id, "method": method, "ok": True, "result": result})
        except Exception as error:
            self.emit({
                "id": request_id,
                "method": method if isinstance(method, str) else "",
                "ok": False,
                "error": str(error),
            })


class _RequestWorkers:
    """Bounded daemon workers keep slow D-Bus sends from freezing the shell."""

    def __init__(self, bridge: QuickshellBridge) -> None:
        self.bridge = bridge
        self.pending: queue.Queue[str] = queue.Queue(maxsize=MAX_PENDING_REQUESTS)
        for index in range(REQUEST_WORKERS):
            threading.Thread(
                target=self._work,
                name=f"blueferry-quickshell-{index}",
                daemon=True,
            ).start()

    def _work(self) -> None:
        while True:
            self.bridge.handle_line(self.pending.get())

    def submit(self, line: str) -> None:
        try:
            self.pending.put_nowait(line)
        except queue.Full:
            self.bridge.emit({
                "id": None,
                "method": "",
                "ok": False,
                "error": "too many pending BlueFerry requests",
            })


def _install_signal_receivers(bridge: QuickshellBridge) -> list[object]:
    bus = get_session_bus()
    common = {
        "dbus_interface": EVENTS_IFACE,
        "bus_name": BUS_NAME,
        "path": OBJECT_PATH,
    }
    return [
        bus.add_signal_receiver(
            lambda _props: bridge.emit_event("history-changed"),
            signal_name="HistoryChanged",
            **common,
        ),
        bus.add_signal_receiver(
            lambda: bridge.emit_event("status-changed"),
            signal_name="StatusChanged",
            **common,
        ),
        bus.add_signal_receiver(
            lambda handle: bridge.emit_event("open-message", str(handle)),
            signal_name="OpenMessageRequested",
            **common,
        ),
    ]


def main() -> int:
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bridge = QuickshellBridge(BackendClient())
    workers = _RequestWorkers(bridge)
    signal_matches = _install_signal_receivers(bridge)
    loop = GLib.MainLoop()

    def read_request(_source: object, condition: int) -> bool:
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            loop.quit()
            return False
        line = sys.stdin.readline()
        if not line:
            loop.quit()
            return False
        workers.submit(line)
        return True

    GLib.io_add_watch(
        sys.stdin.fileno(),
        GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
        read_request,
    )
    try:
        loop.run()
    finally:
        for match in signal_matches:
            remove: Callable[[], object] | None = getattr(match, "remove", None)
            if remove is not None:
                remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
