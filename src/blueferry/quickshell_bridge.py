"""Private stdin bridge from Quickshell to the session D-Bus service.

Quickshell does not provide a generic D-Bus QML type.  Keeping one helper
alive lets the shell use BlueFerry's authenticated D-Bus API without placing
message content, destinations, or contact queries in child-process argv.
"""
from __future__ import annotations

import base64
import json
import queue
import re
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any, TextIO

from blueferry import doctor_report, features_view, phone_link_view, phone_overview
from blueferry.backend_lifecycle import restart_backend
from blueferry.bus import get_session_bus
from blueferry.client import BackendClient, BackendError, TetherUnsupportedError
from blueferry.client_activation import record_client_use
from blueferry.config import FEATURE_SWITCHES
from blueferry.contact_photos import image_type, valid_photo
from blueferry.i18n import _
from blueferry.phone_audio_view import phone_audio
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH, TETHER_IFACE
from blueferry.reconnect_view import ManualReconnect, reconnect_view
from blueferry.service_manager import bluetooth_restart_command
from blueferry.tether_status import TetherStatus

MAX_REQUEST_CHARS = 1_048_576
MAX_PENDING_REQUESTS = 32
REQUEST_WORKERS = 4
# The panel's notification list is compact; the other clients keep 100.
NOTIFICATION_LIMIT = 30
_FEATURE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FEATURE_VARIABLES = {name: variable for name, variable, _default in FEATURE_SWITCHES}


_TETHER_METHODS = frozenset({
    "tether_state", "tether_connect", "tether_disconnect", "tether_configure",
})


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


def _tether_payload(status: TetherStatus) -> dict[str, object]:
    """The decoded tether state plus the shared wording, for the QML side."""
    return {
        **asdict(status),
        "available": True,
        "active": status.active,
        "summary": status.summary(),
    }


def _boolean(args: Mapping[str, Any], name: str) -> bool:
    value = args.get(name)
    if not isinstance(value, bool):
        raise RequestError(f"{name} must be a boolean")
    return value


def features_reply(state: dict[str, Any]) -> dict[str, Any]:
    """``features_view.view`` with the switches as a list of rows.

    Like the thread list, the rows are a plain array; shell.qml's
    ``plainValue`` turns it back into a JavaScript array under Qt 6.12
    (#213). Each row carries the switch ``name`` and ``variable``; the shell words
    the label itself (it loads no translation catalog) and shows the shared
    ``note``, e.g. the ``local.env`` variable an older daemon needs.
    """
    view = features_view.view(state)
    switches = view["switches"]
    items = state.get("items") or {}
    rows = []
    for name, row in switches.items():
        info = items.get(name)
        variable = info.get("variable") if isinstance(info, dict) else None
        rows.append({
            "name": name,
            "variable": str(variable or _FEATURE_VARIABLES.get(name, "")),
            **row,
        })
    return {
        "loaded": view["loaded"] is True,
        "available": view["available"] is True,
        "error": str(view["error"] or ""),
        "notice": str(view["notice"] or ""),
        "restart_pending": view["restart_pending"] is True,
        "restart_notice": str(view["restart_notice"]),
        "switches": rows,
    }


def notifications_reply(snapshot: object, opt_in_hint: str, error: str = "") -> dict[str, Any]:
    """The Notifications list as the panel shows it.

    ``snapshot`` is ``ListNotifications``; the placeholder and the content
    notice come from ``phone_link_view.notifications_view`` like in the
    other clients. Rows are plain text taken verbatim from the iPhone.
    """
    value = snapshot if isinstance(snapshot, Mapping) else {}
    info = {
        "enabled": value.get("enabled") is True,
        "content": value.get("content") is True,
        "error": error,
    }
    rows = [] if error or opt_in_hint else phone_link_view.notification_rows(
        value.get("notifications")
    )[:NOTIFICATION_LIMIT]
    view = phone_link_view.notifications_view(info, opt_in_hint)
    return {
        "off": bool(opt_in_hint),
        "failed": bool(error) and not opt_in_hint,
        "title": view["title"],
        "explanation": view["explanation"],
        "content_hint": view["contentHint"],
        "rows": [
            {key: row[key] for key in ("app", "time", "summary", "body")} for row in rows
        ],
    }


def now_playing_view(snapshot: object) -> dict[str, Any]:
    """Reduce GetNowPlaying for the shell without arrays or translated text.

    The buttons are separate booleans, so nothing depends on how a Qt version
    hands arrays to QML, and the shell, which loads no translation catalog,
    words the placeholders itself. Track text stays verbatim.
    """
    view = phone_overview.now_playing(snapshot)

    def part(key: str) -> Mapping[str, Any]:
        value = snapshot.get(key) if view.visible and isinstance(snapshot, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    track, player = part("track"), part("player")
    title = str(track.get("title") or "")
    artist = str(track.get("artist") or "")
    # AMS availability means the service is connected, not that a player has
    # a track. Reconnects can report an empty, paused Music player with only
    # volume controls. Keep the compact strip hidden until there is content
    # to identify or active playback to control; named paused tracks stay.
    visible = view.visible and bool(view.playing or title.strip() or artist.strip())
    return {
        "visible": visible,
        "playing": visible and view.playing,
        "title": title if visible else "",
        "artist": artist if visible else "",
        "idle": visible and not title and not artist and not player.get("state"),
        **{name: visible and name in view.commands for name in phone_overview.MEDIA_BUTTONS},
    }


class QuickshellBridge:
    """Decode local requests and dispatch them through ``BackendClient``."""

    def __init__(
        self,
        client: BackendClient,
        output: TextIO = sys.stdout,
        *,
        desktop_client: bool = False,
    ) -> None:
        self.client = client
        self.output = output
        self._output_lock = threading.Lock()
        self.desktop_client = desktop_client
        # Host fact, not daemon state: the command that restarts BlueZ on this
        # init system, or "" when unknown. It cannot change while running.
        self.bluetooth_restart_command = bluetooth_restart_command() or ""
        # The notice after "Reconnect", shared with Qt and GTK; it lasts
        # until a status shows the outcome. Workers run in parallel.
        self._manual_reconnect = ManualReconnect()
        self._reconnect_lock = threading.Lock()

    def host_info(self) -> dict[str, str]:
        """Return daemon-independent facts sent to the shell at startup."""
        return {"bluetooth_restart_command": self.bluetooth_restart_command}

    def _reconnect_view(self, status: Mapping[str, Any]) -> dict[str, Any]:
        """Manual reconnect row: shared texts, the outcome of a pressed button."""
        view = reconnect_view(status)
        with self._reconnect_lock:
            error = self._manual_reconnect.follow(status)
            hint = self._manual_reconnect.hint(view)
        return {
            "available": view.available,
            # Qt and GTK hide the button while bluetoothd does not answer.
            "offered": view.offered and status.get("bluez_unresponsive") is not True,
            "attention": view.attention,
            "hint": hint,
            "error": error,
        }

    def _features(self) -> dict[str, Any]:
        try:
            value = self.client.features()
        except BackendError as error:
            # Older daemons: every switch names its local.env variable.
            return features_view.unavailable(features_view.initial(), str(error))
        return features_view.loaded(features_view.initial(), value)

    def _notifications(self) -> dict[str, Any]:
        hint = phone_link_view.feature_hints(self.client.status().to_dict()).get(
            "notifications", ""
        )
        if hint:
            # Switched off: nothing private to read.
            return notifications_reply({}, hint)
        try:
            snapshot = self.client.notifications(NOTIFICATION_LIMIT)
        except BackendError as error:
            return notifications_reply({}, "", str(error) or _("Notifications are unavailable"))
        return notifications_reply(snapshot, "")

    def emit(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._output_lock:
            self.output.write(line)
            self.output.write("\n")
            self.output.flush()

    def emit_event(self, name: str, data: object = None) -> None:
        self.emit({"event": name, "data": data})

    def activation_requested(self, handle: str, _token: str) -> None:
        """Show the shell, opening ``handle`` unless it is empty.

        The activation token stays unused: the window lives in the qs
        process, and QML cannot hand Qt Wayland a token (it only reads
        XDG_ACTIVATION_TOKEN from its own environment).
        """
        self.emit_event("open-message", handle)

    def dispatch(self, method: str, args: Mapping[str, Any]) -> object:
        if method == "client_active" and self.desktop_client:
            record_client_use("quickshell")
            return None
        if method == "status":
            status = self.client.status().to_dict()
            if "phone_audio_route" in status:
                # Quickshell has no gettext; it shows the shared wording.
                status["phone_audio_view"] = phone_audio(status)
            if "phone_reconnect_state" in status:
                # Older daemons cannot reconnect on request: no row.
                status["reconnect_view"] = self._reconnect_view(status)
            return status | self.host_info()
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
        if method == "contact_photo":
            address = _text(args, "address")
            data = valid_photo(self.client.contact_photo(address))
            source = (
                f"data:{image_type(data)};base64,{base64.b64encode(data).decode('ascii')}"
                if data else ""
            )
            return {"address": address, "source": source}
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
                expected_group_token=_text(args, "expected_group_token"),
            )
        if method == "set_group_participants":
            return self.client.set_group_participants(
                _text(args, "thread_key"), _texts(args, "recipients")
            ).to_dict()
        if method == "delete_threads":
            return self.client.delete_threads(_texts(args, "thread_keys"))
        if method == "mark_thread_read":
            return self.client.mark_thread_read(_text(args, "thread_key"))
        if method == "set_thread_starred":
            return self.client.set_thread_starred(
                _text(args, "thread_key"), _boolean(args, "starred")
            )
        if method == "set_notification_policy":
            return self.client.set_notification_policy(_text(args, "policy"))
        if method == "set_contacts_only_notifications":
            return self.client.set_contacts_only_notifications(
                _boolean(args, "enabled")
            )
        if method == "set_ancs_notification_actions":
            return self.client.set_ancs_notification_actions(
                _boolean(args, "enabled")
            )
        if method == "now_playing":
            return now_playing_view(self.client.now_playing())
        if method == "media_command":
            command = _text(args, "command")
            if command not in phone_overview.MEDIA_BUTTONS:
                raise RequestError("unsupported media command")
            self.client.send_media_command(command)
            return None
        if method == "set_media_control":
            return self.client.set_media_control(_boolean(args, "enabled"))
        if method == "set_mpris_player":
            return self.client.set_mpris_player(_boolean(args, "enabled"))
        if method == "set_phone_audio_route":
            route = _text(args, "route")
            if route not in ("pc", "phone"):
                raise RequestError("route must be pc or phone")
            return self.client.set_phone_audio_route(route)
        if method == "set_auto_profile_reset":
            return {
                "profile_reset_auto": self.client.set_auto_profile_reset(
                    _boolean(args, "enabled")
                )
            }
        if method == "set_proximity_lock":
            grace = args.get("grace_seconds")
            if isinstance(grace, bool) or not isinstance(grace, int):
                raise RequestError("grace_seconds must be an integer")
            return self.client.set_proximity_lock(_boolean(args, "enabled"), grace)
        if method == "set_call_history":
            return self.client.set_call_history(
                _boolean(args, "enabled"),
                _boolean(args, "missed_call_notifications"),
            )
        if method == "set_calls_enabled":
            return self.client.set_calls_enabled(_boolean(args, "enabled"))
        if method == "set_contact_photos":
            return self.client.set_contact_photos(_boolean(args, "enabled"))
        if method == "set_otp_autocopy":
            return self.client.set_otp_autocopy(_boolean(args, "enabled"))
        if method == "set_storage_policy":
            return self.client.set_storage_policy(_text(args, "policy"))
        if method == "unlock_storage":
            return self.client.unlock_storage()
        if method == "set_phone_battery_warning":
            return self.client.set_phone_battery_warning(_boolean(args, "enabled"))
        if method == "set_mirror_notification_removals":
            return self.client.set_mirror_notification_removals(_boolean(args, "enabled"))
        if method == "reconnect_phone":
            result = str(self.client.reconnect_phone())
            with self._reconnect_lock:
                error = self._manual_reconnect.answered(result)
                notice = self._manual_reconnect.notice
            return {"result": result, "error": error, "notice": notice}
        if method == "sync_contacts":
            count = int(self.client.sync_contacts())
            return {
                "count": count,
                "text": _("Synced {count} contact destinations").format(count=count),
            }
        if method == "features":
            return features_reply(self._features())
        if method == "set_feature":
            name = _text(args, "name")
            if not _FEATURE_NAME.fullmatch(name):
                raise RequestError("name must be a feature name")
            result = self.client.set_feature(name, _boolean(args, "enabled"))
            state = features_view.with_notice(
                self._features(), features_view.set_notice(result)
            )
            return {"result": str(result), **features_reply(state)}
        if method == "restart_backend":
            restart_backend()
            return None
        if method == "doctor":
            report = doctor_report.run_doctor()
            return doctor_report.view(
                doctor_report.finished_state(doctor_report.idle_state(), report)
            )
        if method == "notifications":
            return self._notifications()
        if method in _TETHER_METHODS:
            return self._tether(method, args)
        raise RequestError(f"unsupported method: {method}")

    def _tether(self, method: str, args: Mapping[str, Any]) -> dict[str, object]:
        if method == "tether_configure":
            enabled = _boolean(args, "enabled")
            autoconnect = _boolean(args, "autoconnect")
        try:
            if method == "tether_connect":
                status = self.client.tether_connect()
            elif method == "tether_disconnect":
                status = self.client.tether_disconnect()
            elif method == "tether_configure":
                status = self.client.tether_configure(enabled, autoconnect)
            else:
                status = self.client.tether_state()
        except TetherUnsupportedError:
            # An older daemon without Tether1: the shell hides the section.
            return {"available": False}
        return _tether_payload(status)

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
        # Focus describes the client at receipt time. It must not wait behind
        # slow backend I/O and then overwrite a newer client's preference.
        if len(line) <= MAX_REQUEST_CHARS:
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                request = None
            if isinstance(request, dict) and request.get("method") == "client_active":
                self.bridge.handle_line(line)  # retain normal validation and widget opt-out
                return
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
        # Content-free: the shell refetches the state with tether_state.
        bus.add_signal_receiver(
            lambda: bridge.emit_event("tether-changed"),
            signal_name="TetherChanged",
            dbus_interface=TETHER_IFACE,
            bus_name=BUS_NAME,
            path=OBJECT_PATH,
        ),
        # Content-free; the shell refetches through now_playing.
        bus.add_signal_receiver(
            lambda: bridge.emit_event("now-playing-changed"),
            signal_name="NowPlayingChanged",
            **common,
        ),
        # Content-free; the shell rereads only while its list is shown.
        bus.add_signal_receiver(
            lambda: bridge.emit_event("notifications-changed"),
            signal_name="NotificationsChanged",
            **common,
        ),
    ]


def _read_requests(stream: TextIO, submit: Callable[[str], None]) -> None:
    while True:
        line = stream.readline(MAX_REQUEST_CHARS + 2)
        if not line:
            return
        if len(line) > MAX_REQUEST_CHARS:
            # Report one oversized request, then discard its remaining chunks.
            submit(line)
            while line and not line.endswith("\n"):
                line = stream.readline(MAX_REQUEST_CHARS + 2)
            continue
        submit(line)


def _quit_on_signals(loop: Any) -> list[int]:
    """End ``loop`` on SIGTERM and SIGHUP, as when Quickshell stops the bridge.

    Without this the default action kills the process before ``main``'s
    cleanup, and a running diagnostics child would outlive the bridge.
    """
    from gi.repository import GLib

    def quit_loop() -> bool:
        loop.quit()
        return GLib.SOURCE_CONTINUE  # main removes the sources

    try:  # PyGObject 3.52+ moved it and deprecated the old name.
        import gi

        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix

        signal_add: Callable[..., int] = GLibUnix.signal_add
    except (ImportError, ValueError, AttributeError):
        # Older GLib lacks GLibUnix or, as on Ubuntu 24.04, its signal_add.
        signal_add = GLib.unix_signal_add
    return [
        signal_add(GLib.PRIORITY_HIGH, number, quit_loop)
        for number in (signal.SIGTERM, signal.SIGHUP)
    ]


def main() -> int:
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    desktop_client = "--desktop-client" in sys.argv[1:]
    bridge = QuickshellBridge(BackendClient(), desktop_client=desktop_client)
    # Delivered before any request, so it survives an unavailable daemon.
    bridge.emit_event("host", bridge.host_info())
    activation = None
    if desktop_client:
        from blueferry.glib_client_activation import ClientActivation

        activation = ClientActivation("quickshell", bridge.activation_requested)
        record_client_use("quickshell")
    workers = _RequestWorkers(bridge)
    signal_matches = _install_signal_receivers(bridge)
    loop = GLib.MainLoop()

    # A dedicated reader drains TextIO's prefetched lines and keeps a partial
    # request from blocking GLib's D-Bus signal dispatch.
    def read_requests() -> None:
        _read_requests(sys.stdin, workers.submit)
        GLib.idle_add(loop.quit)

    threading.Thread(target=read_requests, name="blueferry-stdin", daemon=True).start()
    signal_sources = _quit_on_signals(loop)
    try:
        loop.run()
    finally:
        context = GLib.MainContext.default()
        for source_id in signal_sources:
            source = context.find_source_by_id(source_id)
            if source is not None:
                source.destroy()
        # A running diagnostics child would otherwise outlive the shell.
        doctor_report.terminate_running()
        if activation is not None:
            activation.close()
        for match in signal_matches:
            remove: Callable[[], object] | None = getattr(match, "remove", None)
            if remove is not None:
                remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
