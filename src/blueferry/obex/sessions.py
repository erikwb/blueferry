"""Long-lived MAP + PBAP OBEX sessions.

As recorded in PROTOCOL.md, the iPhone refuses repeat OBEX connects within a
short window. The daemon keeps one MAP session and one PBAP session open
for its lifetime, reopening only on observed failure.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import dbus
import dbus.exceptions

from blueferry import config
from blueferry.bus import get_session_bus, obex
from blueferry.errors import ObexError

log = logging.getLogger(__name__)


class SessionError(ObexError):
    pass


@dataclass(slots=True)
class ObexSession:
    """A live OBEX session against the iPhone (Target = "MAP" or "PBAP")."""

    target: str               # "MAP" or "PBAP"
    path: str                 # /org/bluez/obex/client/session{N}

    @property
    def message_access(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.MessageAccess1")

    @property
    def phonebook(self) -> dbus.Interface:
        return obex(self.path, "org.bluez.obex.PhonebookAccess1")

    @property
    def properties(self) -> dbus.Interface:
        return obex(self.path, "org.freedesktop.DBus.Properties")


def _client() -> dbus.Interface:
    return obex("/org/bluez/obex", "org.bluez.obex.Client1")


def _remove_stale_sessions(targets: set[str]) -> None:
    """Remove only stale sessions for this iPhone and requested profiles."""
    try:
        manager = obex("/", "org.freedesktop.DBus.ObjectManager")
        managed = manager.GetManagedObjects()
    except dbus.exceptions.DBusException as error:
        log.debug("could not inspect existing OBEX sessions: %s", error)
        return
    try:
        client = _client()
    except dbus.exceptions.DBusException as error:
        log.debug("could not access OBEX client for stale cleanup: %s", error)
        return
    for path, interfaces in managed.items():
        props = interfaces.get("org.bluez.obex.Session1")
        if not props:
            continue
        destination = str(props.get("Destination", "")).upper()
        target = str(props.get("Target", "")).upper()
        if destination != config.IPHONE_MAC.upper() or target not in targets:
            continue
        try:
            client.RemoveSession(path, timeout=5.0)
            log.info("removed stale %s session: %s", target, path)
        except dbus.exceptions.DBusException as error:
            log.debug("could not remove stale session %s: %s", path, error)


def _create_session(target: str, *, retry_on_forbidden: bool = True) -> ObexSession:
    log.info("creating OBEX session (Target=%s)", target)
    log.debug("OBEX destination: %s", config.IPHONE_MAC)
    try:
        path = str(_client().CreateSession(
            config.IPHONE_MAC, {"Target": target}, timeout=30.0
        ))
        return ObexSession(target=target, path=path)
    except dbus.exceptions.DBusException as e:
        msg = e.get_dbus_message() or ""
        if retry_on_forbidden and ("Forbidden" in msg or "0x43" in msg):
            log.warning("OBEX %s got Forbidden — removing only matching "
                        "stale sessions and retrying once", target)
            _remove_stale_sessions({target})
            return _create_session(target, retry_on_forbidden=False)
        raise SessionError(f"CreateSession({target}) failed: {e.get_dbus_name()}: {msg}")


class SessionManager:
    """Opens and tracks one MAP and one PBAP session for the daemon lifetime."""

    def __init__(self, on_lost: Callable[[str], None] | None = None) -> None:
        self.map: ObexSession | None = None
        self.pbap: ObexSession | None = None
        self._on_lost = on_lost
        self._signal_matches: list = []
        self._closing = False
        self._monitoring = False

    def set_on_lost(self, callback: Callable[[str], None] | None) -> None:
        """Replace the lifecycle observer before monitoring starts."""
        self._on_lost = callback

    def start_monitoring(self) -> None:
        if self._monitoring:
            return
        bus = get_session_bus()
        self._signal_matches.append(bus.add_signal_receiver(
            self._on_interfaces_removed,
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesRemoved",
            bus_name="org.bluez.obex",
        ))
        self._signal_matches.append(bus.add_signal_receiver(
            self._on_name_owner_changed,
            dbus_interface="org.freedesktop.DBus",
            signal_name="NameOwnerChanged",
            bus_name="org.freedesktop.DBus",
            arg0="org.bluez.obex",
        ))
        self._monitoring = True

    def stop_monitoring(self) -> None:
        for match in self._signal_matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove OBEX lifecycle watch", exc_info=True)
        self._signal_matches.clear()
        self._monitoring = False

    def _on_interfaces_removed(self, path, _interfaces) -> None:
        lost = []
        path = str(path)
        if self.map is not None and path == self.map.path:
            lost.append("MAP")
            self.map = None
        if self.pbap is not None and path == self.pbap.path:
            lost.append("PBAP")
            self.pbap = None
        if lost and not self._closing:
            self._notify_lost("/".join(lost) + " session disappeared")

    def _on_name_owner_changed(self, _name, old_owner, new_owner) -> None:
        if old_owner and not new_owner and (self.map or self.pbap):
            self.map = None
            self.pbap = None
            if not self._closing:
                self._notify_lost("obexd exited")

    def _notify_lost(self, reason: str) -> None:
        log.warning("OBEX connection lost: %s", reason)
        if self._on_lost is not None:
            self._on_lost(reason)

    def report_error(self, error: Exception) -> None:
        """Promote object/service disappearance errors into reconnects."""
        text = str(error)
        if any(marker in text for marker in (
            "UnknownObject", "ServiceUnknown", "NameHasNoOwner", "NoReply",
        )):
            self.map = None
            self.pbap = None
            self._notify_lost(text[:160])

    def open_all(self) -> None:
        self.start_monitoring()
        if self.map is not None and self.pbap is not None:
            return
        # A previous partial open must not leak into this attempt. Otherwise
        # removing it below can emit a loss signal that races the new session.
        if self.map is not None or self.pbap is not None:
            self.close_all()
        _remove_stale_sessions({"MAP", "PBAP"})
        try:
            self.map = _create_session("MAP")
            log.info("MAP session: %s", self.map.path)
            self.pbap = _create_session("PBAP")
            log.info("PBAP session: %s", self.pbap.path)
        except Exception:
            try:
                self.close_all()
            except Exception:
                log.debug("partial OBEX session cleanup failed", exc_info=True)
            raise

    def open_pbap_only(self) -> None:
        """Open only PBAP for a standalone contacts operation.

        The long-running daemon uses open_all(). This path is for CLI fallback
        when no daemon owns the OBEX sessions, and avoids creating a needless
        MAP connection.
        """
        self.start_monitoring()
        _remove_stale_sessions({"PBAP"})
        self.pbap = _create_session("PBAP")
        log.info("PBAP session: %s", self.pbap.path)

    def close_all(self, *, remove_remote: bool = True) -> None:
        """Forget both sessions, optionally asking obexd to remove them first.

        Recovery from an observed transport loss must not call RemoveSession:
        BlueZ 5.87 can crash when that request drives an already-disconnected
        GObex channel into read_packet(). Normal shutdown and partial-open
        cleanup still remove live remote sessions; the next open attempt can
        clean up any surviving stale object after the reconnect delay.
        """
        self._closing = True
        try:
            if not remove_remote:
                log.debug("discarding local OBEX session state after transport loss")
                return
            client = _client()
            for sess in (self.map, self.pbap):
                if sess is None:
                    continue
                try:
                    client.RemoveSession(sess.path, timeout=5.0)
                    log.info("closed %s session: %s", sess.target, sess.path)
                except dbus.exceptions.DBusException as e:
                    log.debug(
                        "RemoveSession(%s): %s", sess.path, e.get_dbus_name()
                    )
        finally:
            self.map = None
            self.pbap = None
            self._closing = False

    # Convenience accessors
    @property
    def map_path(self) -> str:
        if self.map is None:
            raise SessionError("MAP session not open")
        return self.map.path

    @property
    def pbap_path(self) -> str:
        if self.pbap is None:
            raise SessionError("PBAP session not open")
        return self.pbap.path
