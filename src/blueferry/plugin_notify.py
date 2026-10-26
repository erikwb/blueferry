"""Desktop popups requested by plugins (capability ``notify``, PLUGINS.md).

A plugin emits the content-free ``NotifyChanged()`` on its ``Plugin1``
interface on the session bus (ApiVersion 1.5). The daemon checks the
sender, then fetches the popup from that same connection with
``GetPendingNotification()``, so the text never travels on a signal other
processes can read. It shows the popup through its own notification sink,
so the user's policy applies (``none`` silences plugins too, and without
``BLUEFERRY_SHOW_NOTIFICATION_CONTENT`` only the plugin's name is shown).
A click on the popup's button calls the plugin's
``Plugin1.InvokeAction("notify", action_id, "{}")`` on a verified unique
owner, activating the plugin first if needed. An ``open_uri`` in the
answer is checked before opening it.

The daemon still never loads plugin code: it reads manifests (files only)
to learn which bus names may ask for popups, checks that the signal's sender
owns such a name and runs as the same user, re-reads the disabled list on
every signal (disabling is a kill switch) and limits each plugin to a few
popups a minute. Every bus call here is asynchronous.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from blueferry import __version__
from blueferry.plugin_api import (
    CAPABILITY_NOTIFY,
    MAX_REPLY_BYTES,
    METHOD_GET_PENDING_NOTIFICATION,
    METHOD_INVOKE_ACTION,
    OBJECT_PATH,
    SIGNAL_NOTIFY_CHANGED,
    SURFACES_INTERFACE,
)
from blueferry.plugin_api.client import plugin_cache_roots
from blueferry.plugin_api.manifest import PluginManifest, discover
from blueferry.plugin_api.surfaces import (
    MAX_PENDING_NOTIFICATIONS,
    NOTIFY_ITEM_ID,
    Notification,
    SurfaceError,
    checked_open_uri,
    parse_action_result,
    parse_pending,
)
from blueferry.plugin_prefs import disabled_plugins

log = logging.getLogger(__name__)

_DBUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
POPUPS_PER_MINUTE = 6
# NotifyChanged signals one sender may cause lookups for, before it is verified;
# stops any session-bus client from making the daemon scan and ask the bus
# in a loop.
LOOKUPS_PER_MINUTE = 12
# Bound work before replies arrive, including empty/error replies which do
# not consume the popup budget. A burst shares one lookup/fetch per sender.
MAX_PENDING_SENDERS = 16
FETCHES_PER_MINUTE = 24
# Manifests are re-read at most this often (files only, but on the loop).
MANIFEST_TTL_SEC = 30.0
MAX_VERIFIED_SENDERS = 64
ACTION_TIMEOUT_SEC = 60.0
LOOKUP_TIMEOUT_SEC = 2.0
FETCH_TIMEOUT_SEC = 5.0


@dataclass(frozen=True, slots=True)
class PluginPopup:
    plugin_id: str
    plugin_name: str
    bus_name: str
    note: Notification
    plugin_alias: str = ""


def notify_plugins() -> list[PluginManifest]:
    """Manifests with ``notify``; files only, nothing is started. Whether
    one is disabled is checked separately, on every signal."""
    found = discover(blueferry_version=__version__)
    return list(found.with_capability(CAPABILITY_NOTIFY))


def gio_open_uri(uri: str) -> None:
    from gi.repository import Gio

    Gio.AppInfo.launch_default_for_uri_async(uri, None, None, None, None)


class PluginPopups:
    """Watch Plugin1.NotifyChanged, fetch the popup, hand it to ``show``."""

    def __init__(
        self,
        bus,
        *,
        show: Callable[[PluginPopup], None],
        plugins: Callable[[], Sequence[PluginManifest]] = notify_plugins,
        disabled: Callable[[], frozenset[str]] = disabled_plugins,
        open_uri: Callable[[str], None] = gio_open_uri,
        uid: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._show = show
        self._plugins = plugins
        self._disabled = disabled
        self._open_uri = open_uri
        self._uid = os.getuid() if uid is None else uid
        self._clock = clock
        self._recent: dict[str, deque[float]] = {}
        self._lookups: dict[str, deque[float]] = {}
        self._fetches: dict[str, deque[float]] = {}
        self._pending: dict[str, object] = {}
        self._fetch_again: set[str] = set()
        self._stopped = False
        self._generation = 0
        self._manifests: list[PluginManifest] = []
        self._manifests_at = float("-inf")
        # Cache the candidate, not ownership: a live connection can release
        # a well-known name without losing its unique bus name.
        self._verified: dict[str, PluginManifest] = {}
        self._match = None

    def start(self) -> None:
        if self._match is not None:
            return
        self._stopped = False
        try:
            self._match = self._bus.add_signal_receiver(
                self._on_changed, dbus_interface=SURFACES_INTERFACE,
                signal_name=SIGNAL_NOTIFY_CHANGED, path=OBJECT_PATH, sender_keyword="sender",
            )
        except Exception:
            log.warning("could not watch plugin popups; they stay off")

    def stop(self) -> None:
        self._stopped = True
        self._generation += 1
        self._pending.clear()
        self._fetch_again.clear()
        self._verified.clear()
        if self._match is None:
            return
        try:
            self._match.remove()
        except Exception:
            log.debug("could not remove the plugin popup watch", exc_info=True)
        self._match = None

    # ---- incoming ---------------------------------------------------------------

    def _on_changed(self, *_args, sender=None) -> None:
        """A plugin says a popup waits; the signal itself carries nothing."""
        if not sender or self._stopped:
            return
        sender = str(sender)
        if sender in self._pending:
            self._fetch_again.add(sender)
            return
        if len(self._pending) >= MAX_PENDING_SENDERS:
            return
        known = self._verified.get(sender)
        if known is not None:
            if (self._enabled(known.id, known.bus_name)
                    and self._room(self._recent, known.id, POPUPS_PER_MINUTE)
                    and self._room(self._fetches, known.id, FETCHES_PER_MINUTE)
                    and self._admit(self._lookups, sender, FETCHES_PER_MINUTE)):
                token = self._pending[sender] = object()
                self._match_owner(sender, [known], token)
            return
        if not self._admit(self._lookups, sender, LOOKUPS_PER_MINUTE):
            log.info("ignoring repeated popups from an unverified sender")
            return
        token = self._pending[sender] = object()
        self._match_owner(sender, self._candidates(), token)

    def _enabled(self, plugin_id: str, bus_name: str) -> bool:
        return not self._stopped and any(
            plugin.id == plugin_id and plugin.bus_name == bus_name
            for plugin in self._candidates()
        )

    def _finish(self, sender: str, token: object) -> None:
        if self._pending.get(sender) is token:
            del self._pending[sender]
            self._fetch_again.discard(sender)

    def _pending_call(self, sender: str, token: object, *args, **kwargs) -> None:
        try:
            self._bus.call_async(*args, **kwargs)
        except Exception:
            self._finish(sender, token)
            log.debug("could not dispatch a plugin popup request", exc_info=True)

    def _candidates(self) -> list[PluginManifest]:
        """Enabled notify plugins: manifests cached briefly, the disabled
        list read fresh, so a plugin stops at once when the user disables it."""
        now = self._clock()
        if now - self._manifests_at >= MANIFEST_TTL_SEC:
            try:
                self._manifests = list(self._plugins())
            except Exception:
                log.debug("could not read plugin manifests", exc_info=True)
                self._manifests = []
            self._manifests_at = now
        try:
            disabled = self._disabled()
        except Exception:
            log.debug("could not read the disabled plugins", exc_info=True)
            return []
        return [plugin for plugin in self._manifests if plugin.id not in disabled]

    def _room(self, table: dict[str, deque[float]], key: str, limit: int) -> bool:
        now = self._clock()
        window = table.get(key)
        if window is None:
            return True
        while window and now - window[0] > 60:
            window.popleft()
        return len(window) < limit

    def _admit(self, table: dict[str, deque[float]], key: str, limit: int) -> bool:
        now = self._clock()
        if not self._room(table, key, limit):
            return False
        window = table.setdefault(key, deque())
        window.append(now)
        if len(table) > 256:
            # One-shot senders never come back to empty their window: drop
            # every entry whose newest call is older than the window.
            for stale in [name for name, times in table.items()
                          if not times or now - times[-1] > 60]:
                del table[stale]
            while len(table) > 256:
                table.pop(next(iter(table)))
        return True

    def _match_owner(self, sender: str, candidates: list[PluginManifest], token: object) -> None:
        """Ask the bus, one name at a time, which plugin owns ``sender``."""
        if self._pending.get(sender) is not token:
            return
        if not candidates:
            self._verified.pop(sender, None)
            self._finish(sender, token)
            log.info("ignoring a popup from a sender that is no enabled notify plugin")
            return
        plugin, rest = candidates[0], candidates[1:]

        def owner(name) -> None:
            if self._pending.get(sender) is not token:
                return
            if str(name) == sender:
                self._check_user(sender, plugin, token)
            else:
                self._match_owner(sender, rest, token)

        self._pending_call(
            sender, token,
            *_DBUS, "GetNameOwner", "s", (plugin.bus_name,), owner,
            lambda _error: self._match_owner(sender, rest, token),
            timeout=LOOKUP_TIMEOUT_SEC,
        )

    def _check_user(self, sender: str, plugin: PluginManifest, token: object) -> None:
        def checked(uid) -> None:
            if self._pending.get(sender) is not token:
                return
            if int(uid) != self._uid:
                self._finish(sender, token)
                log.warning("ignoring a popup from a plugin run by another user")
                return
            self._verified[sender] = plugin
            while len(self._verified) > MAX_VERIFIED_SENDERS:
                self._verified.pop(next(iter(self._verified)))
            self._fetch(sender, plugin, MAX_PENDING_NOTIFICATIONS, token)

        self._pending_call(
            sender, token,
            *_DBUS, "GetConnectionUnixUser", "s", (sender,), checked,
            lambda _error: self._finish(sender, token),
            timeout=LOOKUP_TIMEOUT_SEC,
        )

    def _fetch(self, sender: str, plugin: PluginManifest, left: int, token: object) -> None:
        """GetPendingNotification on the verified connection itself (its
        unique name), so the popup comes from the process that was checked."""
        if self._pending.get(sender) is not token:
            return
        if left <= 0 or not self._enabled(plugin.id, plugin.bus_name):
            self._finish(sender, token)
            return
        if not self._room(self._recent, plugin.id, POPUPS_PER_MINUTE):
            self._finish(sender, token)
            log.info("plugin popup rate limit reached; leaving popups unfetched")
            return
        if not self._admit(self._fetches, plugin.id, FETCHES_PER_MINUTE):
            self._finish(sender, token)
            return
        self._fetch_again.discard(sender)

        def replied(text) -> None:
            if self._pending.get(sender) is not token:
                return
            if not isinstance(text, str) or len(text.encode("utf-8", "surrogatepass")) > (
                    MAX_REPLY_BYTES):
                self._finish(sender, token)
                log.info("a plugin answered GetPendingNotification with an unusable reply")
                return
            try:
                note, more = parse_pending(text)
            except SurfaceError:
                self._finish(sender, token)
                log.info("a plugin answered GetPendingNotification with garbage")
                return
            if note is not None:
                self._deliver(plugin, note)
            if more or sender in self._fetch_again:
                self._fetch(sender, plugin, left - 1, token)
            else:
                self._finish(sender, token)

        def failed(error) -> None:
            if self._pending.get(sender) is not token:
                return
            self._finish(sender, token)
            name = getattr(error, "get_dbus_name", lambda: "")() or type(error).__name__
            log.info("could not fetch a plugin popup: %s", name)

        self._pending_call(
            sender, token,
            sender, OBJECT_PATH, SURFACES_INTERFACE, METHOD_GET_PENDING_NOTIFICATION, "",
            (), replied, failed, timeout=FETCH_TIMEOUT_SEC,
        )

    def _deliver(self, plugin: PluginManifest, note: Notification) -> None:
        if not self._enabled(plugin.id, plugin.bus_name):
            return  # disabled while the popup was on its way
        if not self._admit(self._recent, plugin.id, POPUPS_PER_MINUTE):
            log.info("plugin popup rate limit reached; dropping one")
            return
        try:
            self._show(PluginPopup(plugin.id, plugin.name, plugin.bus_name, note, plugin.alias))
        except Exception:
            log.exception("showing a plugin popup failed")

    # ---- the popup's button -----------------------------------------------------

    def invoke(self, popup: PluginPopup) -> None:
        """The user clicked the popup's action button."""
        if not popup.note.has_action or not self._enabled(popup.plugin_id, popup.bus_name):
            return
        generation = self._generation

        def current() -> bool:
            return generation == self._generation and self._enabled(popup.plugin_id, popup.bus_name)

        def failed(error) -> None:
            name = getattr(error, "get_dbus_name", lambda: "")() or type(error).__name__
            log.info("plugin popup action failed: %s", name)

        def call(destination, path, interface, method, signature, args, reply, failure=failed,
                 *, timeout=LOOKUP_TIMEOUT_SEC) -> None:
            if not current():
                return
            try:
                self._bus.call_async(destination, path, interface, method, signature,
                                     args, reply, failure, timeout=timeout)
            except Exception as error:
                failure(error)

        def replied(text) -> None:
            if not current():
                return
            if not isinstance(text, str) or len(text.encode("utf-8", "surrogatepass")) > (
                    MAX_REPLY_BYTES):
                log.info("a plugin answered a popup click with an unusable reply")
                return
            try:
                result = parse_action_result(text)
            except SurfaceError:
                log.info("a plugin answered a popup click with garbage")
                return
            uri = checked_open_uri(
                result.open_uri, plugin_cache_roots(popup.plugin_id, popup.plugin_alias),
                uid=self._uid,
            )
            if result.ok and uri:
                try:
                    self._open_uri(uri)
                except Exception:
                    log.info("could not open what the plugin asked for")
            elif result.open_uri and not uri:
                log.info("refused a plugin open_uri outside http(s) and its cache")

        def owner_found(owner) -> None:
            if not current():
                return
            owner = str(owner)

            def checked(uid) -> None:
                if not current():
                    return
                if int(uid) != self._uid:
                    log.warning("ignoring a popup action for a plugin run by another user")
                    return
                # Pin the verified connection for the call and its reply.
                # A name handover cannot redirect the action or its URI.
                call(owner, OBJECT_PATH, SURFACES_INTERFACE, METHOD_INVOKE_ACTION, "sss",
                     (NOTIFY_ITEM_ID, popup.note.action_id, "{}"), replied,
                     timeout=ACTION_TIMEOUT_SEC)

            call(*_DBUS, "GetConnectionUnixUser", "s", (owner,), checked)

        def lookup(*, activate: bool) -> None:
            def missing(error) -> None:
                name = getattr(error, "get_dbus_name", lambda: "")()
                if activate and name == "org.freedesktop.DBus.Error.NameHasNoOwner":
                    # Activate without handing over the action payload, then
                    # resolve and check the resulting owner exactly once.
                    call(*_DBUS, "StartServiceByName", "su", (popup.bus_name, 0),
                         lambda _result: lookup(activate=False), timeout=ACTION_TIMEOUT_SEC)
                else:
                    failed(error)

            call(*_DBUS, "GetNameOwner", "s", (popup.bus_name,), owner_found, missing)

        lookup(activate=True)
