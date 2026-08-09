"""MAP MNS event subscription and asynchronous bMessage retrieval."""
from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import dbus

from blueferry.bus import get_session_bus, obex
from blueferry.events import SmsEvent, normalize_phone, parse_map_timestamp
from blueferry.limits import MAX_BMESSAGE_BYTES
from blueferry.obex.bmessage import parse as parse_bmessage
from blueferry.obex.sessions import SessionManager
from blueferry.obex.transfer import wait_for_transfer

log = logging.getLogger(__name__)

EventCallback = Callable[[SmsEvent], None]
SubmitObex = Callable[..., object]
FETCH_TIMEOUT_SEC = 120


def _mkstemp_path(prefix: str, suffix: str) -> Path:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(path)


def _fetch_bmessage(message_path: str, target: Path):
    """Download and parse one Message1 object on the OBEX worker."""
    try:
        ret = obex(message_path, "org.bluez.obex.Message1").Get(
            str(target), False, timeout=30.0
        )
        if isinstance(ret, tuple | list):
            transfer_path = str(ret[0])
            initial = dict(ret[1]) if len(ret) > 1 else {}
        else:
            transfer_path = str(ret)
            initial = {}

        def check_size() -> None:
            if target.exists() and target.stat().st_size > MAX_BMESSAGE_BYTES:
                raise RuntimeError(
                    f"message transfer exceeds {MAX_BMESSAGE_BYTES} byte safety limit"
                )

        wait_for_transfer(
            transfer_path,
            initial_status=str(initial.get("Status", "queued")),
            timeout_s=FETCH_TIMEOUT_SEC,
            property_timeout_s=10.0,
            check_progress=check_size,
        )

        # obexd can remove the transfer just before its local file is visible.
        for _ in range(20):
            if target.exists() and target.stat().st_size > 0:
                break
            time.sleep(0.1)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("message transfer produced no bMessage file")
        check_size()
        return parse_bmessage(target.read_text(errors="replace"))
    finally:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


class MapEventListener:
    """Subscribe to MAP push events and dispatch normalized SmsEvents."""

    def __init__(
        self,
        sessions: SessionManager,
        on_sms: EventCallback,
        *,
        submit_obex: SubmitObex,
        resolve_contact: Callable[[str | None], str | None] = lambda _: None,
    ) -> None:
        self.sessions = sessions
        self.on_sms = on_sms
        self.submit_obex = submit_obex
        self.resolve_contact = resolve_contact
        self._signal_match = None
        self._running = False

    def start(self) -> None:
        manager = dbus.Interface(
            get_session_bus().get_object("org.bluez.obex", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        self._signal_match = manager.connect_to_signal(
            "InterfacesAdded", self._on_interfaces_added
        )
        self._running = True
        log.info("MAP MNS listener started (filtering on %s)",
                 self.sessions.map_path)

    def stop(self) -> None:
        self._running = False
        if self._signal_match is not None:
            try:
                self._signal_match.remove()
            except Exception:
                log.debug("could not remove MAP event watch", exc_info=True)
            self._signal_match = None
        log.info("MAP MNS listener stopped")

    def _on_interfaces_added(self, path, ifaces) -> None:
        path_s = str(path)
        session = self.sessions.map
        if session is None or not path_s.startswith(f"{session.path}/"):
            return
        if "org.bluez.obex.Message1" not in ifaces:
            return

        props = dict(ifaces["org.bluez.obex.Message1"])
        handle = path_s.rsplit("/", 1)[-1]
        log.info(
            "new Message1 at %s (Status=%s Type=%s Size=%s) — queuing body fetch",
            handle, props.get("Status"), props.get("Type"), props.get("Size"),
        )
        target = _mkstemp_path("blueferry_msg_", ".bmsg")
        try:
            self.submit_obex(
                lambda: _fetch_bmessage(path_s, target),
                on_success=lambda parsed: self._fetched(
                    handle, path_s, props, parsed
                ),
                on_error=lambda error: self._fetch_failed(
                    handle, error
                ),
            )
        except Exception as error:
            target.unlink(missing_ok=True)
            self._fetch_failed(handle, error)

    def _fetched(self, handle, message_path, props, parsed) -> None:
        if not self._running:
            return
        sender_raw = parsed.sender_address
        contact = self.resolve_contact(sender_raw) if sender_raw else None
        event = SmsEvent(
            kind="sms_received",
            handle=handle,
            sender_address=sender_raw,
            sender_phone_norm=normalize_phone(sender_raw),
            contact_name=contact,
            body=parsed.body,
            timestamp=parse_map_timestamp(props.get("Timestamp")),
            is_read=str(parsed.status or "").upper() == "READ",
            message_path=message_path,
        )
        log.info("sms_received (%d-char body)", len(event.body or ""))
        self._emit(event)

    def _fetch_failed(self, handle, error) -> None:
        if not self._running:
            return
        # Without a parsed address or body this record cannot be displayed,
        # correlated, replied to, or marked read safely. Do not retain a
        # content-free artifact merely because a transfer object existed.
        log.warning("body fetch failed for %s; discarding event: %s", handle, error)

    def _emit(self, event: SmsEvent) -> None:
        try:
            self.on_sms(event)
        except Exception:
            log.exception("on_sms callback raised")
