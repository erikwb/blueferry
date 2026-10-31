"""Plugin side of the contract: export Plugin1 (and a capability) on D-Bus.

A plugin is its own process with its own session-bus name. Subclass
:class:`PluginService` (or :class:`PhotosService`, and for the ApiVersion 1.2
surfaces :class:`CardService`, :class:`ShareService`, :class:`NotifyService`,
which combine by multiple inheritance and export everything on Plugin1),
implement the hooks and call :func:`run`. Slow work (network, disk) runs on a worker thread; replies
go back through the GLib main loop, so the plugin keeps answering Status()
while a download runs. The service exits after ``idle_seconds`` without
calls; D-Bus activation starts it again on the next call.

Workers come from one bounded pool per service (:data:`MAX_WORKERS`
threads); more than :data:`MAX_IN_FLIGHT` unanswered calls are refused as
rate-limited instead of queueing without end.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import dbus
import dbus.service

from . import (
    API_MINOR,
    API_VERSION,
    HOST_BUS_NAME,
    LOGIN_PROVIDERS,
    MAX_RECENT_PHOTOS,
    MAX_REPLY_BYTES,
    OBJECT_PATH,
    PHOTOS_INTERFACE,
    PLUGIN_INTERFACE,
    config_flow,
    logs,
    surfaces,
)
from .config import ConfigError, masked, parse_update, validate
from .manifest import PluginManifest

log = logging.getLogger(__name__)

# Per-caller budget; a plugin is local and single-user, this only stops a
# runaway client from turning it into a download loop.
CALLS_PER_MINUTE = 120
# Callers remembered for the budget; one-shot callers are forgotten once
# their window is empty.
MAX_CALLERS = 256
# Worker threads per service, and calls that may wait for one.
MAX_WORKERS = 4
MAX_IN_FLIGHT = 32
Schedule = Callable[[Callable[[], object]], object]


class PluginCallError(dbus.exceptions.DBusException):
    _dbus_error_name = "io.weirdware.BlueFerry.Plugin.Error.Failed"


class RateLimitedError(dbus.exceptions.DBusException):
    _dbus_error_name = "io.weirdware.BlueFerry.Plugin.Error.RateLimited"


class AccessDeniedError(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.AccessDenied"


def _idle_add(callback: Callable[[], object]) -> object:
    from gi.repository import GLib

    def once() -> bool:
        callback()
        return False

    return GLib.idle_add(once)


class PluginService(dbus.service.Object):
    """Plugin1: GetInfo(), Status() and the optional GetConfig()/SetConfig()."""

    def __init__(
        self,
        manifest: PluginManifest,
        bus: Any = None,
        *,
        to_main: Schedule = _idle_add,
        start_worker: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(bus, OBJECT_PATH) if bus is not None else super().__init__()
        self.manifest = manifest
        self._to_main = to_main
        self._executor: ThreadPoolExecutor | None = None
        self._start_worker = start_worker or self._submit
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}
        self.last_activity = clock()
        # Calls whose worker has not replied yet; run() never idles out then.
        self.in_flight = 0

    # ---- hooks -----------------------------------------------------------

    def status(self) -> dict[str, object]:
        """``{"state": ok|unconfigured|error|busy, "detail": str, ...}``."""
        return {"state": "ok"}

    def config_values(self) -> dict[str, object]:
        """Blocking; worker thread. Current settings by manifest key.

        For a ``secret`` field return anything truthy when one is stored
        (``True`` is enough); the base class never sends it out.
        """
        raise PluginCallError("this plugin has no settings")

    def apply_config(self, values: dict[str, object]) -> None:
        """Blocking; worker thread. Store validated settings.

        ``values`` holds every non-secret field and only the secrets the
        user entered anew; keep the stored secret for a missing key. Raise
        :class:`~blueferry.plugin_api.config.ConfigError` to reject one field
        (for example a key the server refuses).
        """
        raise PluginCallError("this plugin has no settings")

    def test_config(
        self, values: dict[str, object],
    ) -> config_flow.ConfigTestResult | dict[str, object]:
        """Blocking; worker thread. ApiVersion 1.3, with ``ConfigTest=true``.

        Check ``values`` (validated like for :meth:`apply_config`; a secret
        the user did not retype is missing, use the stored one) against the
        server without storing anything. Return ``ConfigTestResult(ok,
        message)``, e.g. ``"Connected as anna to Nextcloud 31"``, or raise
        :class:`~blueferry.plugin_api.config.ConfigError` for one field.
        """
        raise PluginCallError("this plugin cannot test its settings")

    def config_login(
        self, provider: str, values: dict[str, object],
    ) -> config_flow.LoginStep | dict[str, object]:
        """Blocking; worker thread. ApiVersion 1.3, with ``ConfigLogin=``.

        Start the browser sign-in for ``provider`` (the manifest's value).
        ``values`` are the form's typed non-secret values, checked against
        the schema but not stored. Return ``LoginStep("open", login_id=…,
        open_uri=…)``; the client opens the URI and polls
        :meth:`config_login_status`. Store what the provider hands over
        (server, user, app password) yourself, never send it back.
        """
        raise PluginCallError("this plugin has no browser sign-in")

    def config_login_status(self, login_id: str) -> config_flow.LoginStep | dict[str, object]:
        """Blocking; worker thread. ``pending``, then ``done`` (stored;
        message "Connected as …"), ``error``, ``expired`` or ``cancelled``."""
        raise PluginCallError("this plugin has no browser sign-in")

    def config_login_cancel(self, login_id: str) -> None:
        """Blocking; worker thread. Stop the flow; no answer expected."""

    # ---- helpers ---------------------------------------------------------

    def _submit(self, work: Callable[[], None]) -> None:
        """The default worker: a bounded pool, created on first use."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="blueferry-plugin-worker",
            )
        self._executor.submit(work)

    def _authorize(self, sender: str | None) -> None:
        """Only the service's Unix user may invoke an exported plugin.

        Ask the bus about the message's unique sender, never a well-known
        name whose owner can change. Check each call so exporting the object
        on another connection cannot reuse credentials from an earlier bus.
        An unexported object has only in-process callers (the SDK harness).
        """
        connection = getattr(self, "_connection", None)
        if connection is None:
            return
        identity = str(sender or "")
        if not identity.startswith(":"):
            raise AccessDeniedError("D-Bus caller identity is unavailable")
        try:
            uid = int(connection.call_blocking(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetConnectionUnixUser", "s", (identity,),
                timeout=1.0,
            ))
        except Exception:
            raise AccessDeniedError("D-Bus caller credentials could not be verified") from None
        if uid != os.getuid():
            raise AccessDeniedError("D-Bus caller is not authorized")

    def admit(self, sender: str | None) -> None:
        """Authorize and count a call before running hooks or queuing work."""
        self._authorize(sender)
        now = self._clock()
        self.last_activity = now
        key = str(sender)
        window = self._calls.get(key)
        if window is None:
            if len(self._calls) >= MAX_CALLERS:
                self._forget_callers(now)
            window = self._calls[key] = deque()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= CALLS_PER_MINUTE:
            raise RateLimitedError("too many calls")
        if self.in_flight >= MAX_IN_FLIGHT:
            raise RateLimitedError("too many calls at once")
        window.append(now)

    def _forget_callers(self, now: float) -> None:
        for name in [name for name, times in self._calls.items()
                     if not times or now - times[-1] > 60]:
            del self._calls[name]
        while len(self._calls) >= MAX_CALLERS:
            # Still full of active callers: drop the oldest-registered one.
            self._calls.pop(next(iter(self._calls)))

    def run_async(
        self,
        work: Callable[[], str],
        reply: Callable[[str], None],
        error: Callable[[Exception], None],
    ) -> None:
        """Run ``work`` off the main loop and reply on it."""
        self.in_flight += 1

        def finish() -> None:
            self.in_flight -= 1
            self.last_activity = self._clock()

        def worker() -> None:
            try:
                result = work()
                if len(result.encode("utf-8", "surrogatepass")) > MAX_REPLY_BYTES:
                    raise PluginCallError("reply too large")
            except Exception as failure:  # reported to the caller, not raised
                message = str(failure) if isinstance(failure, PluginCallError) else (
                    type(failure).__name__
                )
                log.info("plugin call failed: %s", message)

                def failed() -> None:
                    finish()
                    error(PluginCallError(message))

                self._to_main(failed)
                return

            def done() -> None:
                finish()
                reply(result)

            self._to_main(done)

        self._start_worker(worker)

    # ---- Plugin1 ----------------------------------------------------------

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s", sender_keyword="sender",
    )
    def GetInfo(self, sender=None) -> str:
        self.admit(sender)
        return json.dumps({
            "id": self.manifest.id,
            "name": self.manifest.name,
            "version": self.manifest.version,
            "api_version": API_VERSION,
            "api_minor": API_MINOR,
            "capabilities": list(self.manifest.capabilities),
        })

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s", sender_keyword="sender",
    )
    def Status(self, sender=None) -> str:
        self.admit(sender)
        return json.dumps(self.status())


    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def GetConfig(self, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(self._get_config, reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="s", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def SetConfig(self, update, reply, error, sender=None) -> None:
        self.admit(sender)
        text = str(update)
        self.run_async(lambda: self._set_config(text), reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="s", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def TestConfig(self, update, reply, error, sender=None) -> None:
        self.admit(sender)
        text = str(update)
        self.run_async(lambda: self._test_config(text), reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="ss", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def ConfigLogin(self, provider, values, reply, error, sender=None) -> None:
        self.admit(sender)
        name, text = str(provider), str(values)
        self.run_async(lambda: self._config_login(name, text), reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="s", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def ConfigLoginStatus(self, login_id, reply, error, sender=None) -> None:
        self.admit(sender)
        ident = str(login_id)

        def work() -> str:
            self._login_provider()
            if not config_flow.valid_login_id(ident):
                raise PluginCallError("not a sign-in id")
            return _flow_json(lambda: config_flow.login_step_json(
                self.config_login_status(ident), start=False,
            ))

        self.run_async(work, reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="s", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def ConfigLoginCancel(self, login_id, reply, error, sender=None) -> None:
        self.admit(sender)
        ident = str(login_id)

        def work() -> str:
            self._login_provider()
            if not config_flow.valid_login_id(ident):
                raise PluginCallError("not a sign-in id")
            self.config_login_cancel(ident)
            return json.dumps({"ok": True})

        self.run_async(work, reply, error)

    def _fields(self):
        if not self.manifest.config:
            raise PluginCallError("this plugin has no settings")
        return self.manifest.config

    def _get_config(self) -> str:
        fields = self._fields()
        return json.dumps({"values": masked(fields, self.config_values())})

    def _set_config(self, text: str) -> str:
        fields = self._fields()
        try:
            update = parse_update(text)
        except ConfigError as failure:
            return json.dumps({"ok": False, "errors": {failure.field: failure.message}})
        values, errors = validate(fields, self.config_values(), update)
        if errors:
            return json.dumps({"ok": False, "errors": errors})
        try:
            self.apply_config(values)
        except ConfigError as failure:
            return json.dumps({"ok": False, "errors": {failure.field: failure.message}})
        log.info("plugin settings saved (%d fields)", len(values))
        return json.dumps({"ok": True})

    def _test_config(self, text: str) -> str:
        fields = self._fields()
        if not self.manifest.config_test:
            raise PluginCallError("this plugin cannot test its settings")
        try:
            update = parse_update(text)
        except ConfigError as failure:
            return config_flow.config_test_json(config_flow.ConfigTestResult(
                False, failure.message, {failure.field: failure.message},
            ))
        values, errors = validate(fields, self.config_values(), update)
        if errors:
            return config_flow.config_test_json(config_flow.ConfigTestResult(
                False, "Check the marked settings.", errors,
            ))
        try:
            result = self.test_config(values)
        except ConfigError as failure:
            result = config_flow.ConfigTestResult(
                False, failure.message, {failure.field: failure.message} if failure.field else {},
            )
        log.info("plugin settings tested")
        return _flow_json(lambda: config_flow.config_test_json(result))

    def _login_provider(self) -> str:
        provider = self.manifest.config_login
        if not provider:
            raise PluginCallError("this plugin has no browser sign-in")
        return provider

    def _config_login(self, provider: str, text: str) -> str:
        fields = self._fields()
        if provider != self._login_provider() or provider not in LOGIN_PROVIDERS:
            raise PluginCallError("this plugin has no such sign-in")
        try:
            update = parse_update(text)
        except ConfigError as failure:
            raise PluginCallError(failure.message) from None
        secrets = {field.key for field in fields if field.secret}
        typed = {key: value for key, value in update.items() if key not in secrets}
        values, errors = validate(fields, self.config_values(), typed)
        # The sign-in fills the secrets, so a missing one is no error here.
        reasons = {key: reason for key, reason in errors.items() if key not in secrets}
        if reasons:
            key, reason = next(iter(reasons.items()))
            return config_flow.login_step_json(
                config_flow.LoginStep("error", f"{key}: {reason}"), start=True,
            )
        log.info("plugin sign-in started")
        return _flow_json(lambda: config_flow.login_step_json(
            self.config_login(provider, values), start=True,
        ))


def _flow_json(serialise: Callable[[], str]) -> str:
    """A hook result that breaks the contract fails with the reason."""
    try:
        return serialise()
    except (config_flow.FlowError, TypeError) as error:
        raise PluginCallError(str(error)) from None


class PhotosService(PluginService):
    """Photos1: ListRecent, FetchOriginal and the content-free Changed()."""

    def list_recent(self, limit: int) -> list[dict[str, object]]:
        """Blocking; runs on a worker thread. Newest first."""
        raise NotImplementedError

    def fetch_original(self, photo_id: str) -> str:
        """Blocking; runs on a worker thread. Return a local path."""
        raise NotImplementedError

    @dbus.service.method(
        PHOTOS_INTERFACE, in_signature="u", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def ListRecent(self, limit, reply, error, sender=None) -> None:
        self.admit(sender)
        bounded = max(1, min(int(limit), MAX_RECENT_PHOTOS))
        self.run_async(lambda: json.dumps(self.list_recent(bounded)), reply, error)

    @dbus.service.method(
        PHOTOS_INTERFACE, in_signature="s", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def FetchOriginal(self, photo_id, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(lambda: str(self.fetch_original(str(photo_id))), reply, error)

    @dbus.service.signal(PHOTOS_INTERFACE, signature="")
    def Changed(self) -> None:
        """Something changed; clients call ListRecent again. No content."""


def _action_args(text: str) -> dict[str, object]:
    if len(text.encode("utf-8", "surrogatepass")) > surfaces.MAX_ARGS_BYTES:
        raise PluginCallError("action arguments are too large")
    try:
        value = json.loads(text or "{}")
    except ValueError:
        raise PluginCallError("action arguments are not JSON") from None
    if not isinstance(value, dict):
        raise PluginCallError("action arguments must be a JSON object")
    return value


class _ActionMixin(PluginService):
    """Plugin1.InvokeAction, shared by the card and the popup button."""

    def invoke_action(
        self, item_id: str, action_id: str, args: dict[str, object],
    ) -> surfaces.ActionResult | dict[str, object]:
        """Blocking; worker thread. A click on a card or popup button.

        ``item_id`` is the card item's id, or ``"notify"`` for the action
        button of a popup sent with :meth:`NotifyService.emit_notify`.
        """
        raise PluginCallError("this plugin has no actions")

    def _invoke(self, item_id: str, action_id: str, args_text: str) -> str:
        if not surfaces.valid_id(item_id) or not surfaces.valid_id(action_id):
            raise PluginCallError("not an action id")
        args = _action_args(args_text)
        return surfaces.result_json(self.invoke_action(item_id, action_id, args))

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="sss", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def InvokeAction(self, item_id, action_id, args, reply, error, sender=None) -> None:
        self.admit(sender)
        item, action, text = str(item_id), str(action_id), str(args)
        self.run_async(lambda: self._invoke(item, action, text), reply, error)


class CardService(_ActionMixin):
    """Capability ``card``: items for the phone card's "From Plugins" section.

    Override :meth:`card_items` and :meth:`invoke_action`; call
    :meth:`emit_card_changed` (from any thread) when the items change.
    """

    def card_items(self) -> list[surfaces.CardItem] | list[dict[str, object]]:
        """Blocking; worker thread. At most 8 items, 3 actions each."""
        return []

    def emit_card_changed(self) -> None:
        """Ask BlueFerry to fetch the items again; safe from any thread."""
        self._to_main(self.CardChanged)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def GetCardItems(self, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(lambda: surfaces.card_items_json(self.card_items()), reply, error)

    @dbus.service.signal(PLUGIN_INTERFACE, signature="")
    def CardChanged(self) -> None:
        """The card items changed; BlueFerry calls GetCardItems. No content."""


class ShareService(PluginService):
    """Capability ``share``: destinations for BlueFerry's "Send to…"."""

    def share_targets(self) -> list[surfaces.ShareTarget] | list[dict[str, object]]:
        """Blocking; worker thread."""
        return []

    def send_files(
        self, target_id: str, paths: list[str],
    ) -> surfaces.SendResult | dict[str, object]:
        """Blocking; worker thread. Start the transfer and return quickly;
        report a long one as a card item (and ``emit_card_changed``)."""
        raise PluginCallError("this plugin cannot send files")

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def ShareTargets(self, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(lambda: surfaces.share_targets_json(self.share_targets()), reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="sas", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def SendFiles(self, target_id, paths, reply, error, sender=None) -> None:
        self.admit(sender)
        target = str(target_id)
        files = [str(path) for path in list(paths)[: surfaces.MAX_SHARE_FILES + 1]]

        def work() -> str:
            if not surfaces.valid_id(target):
                raise PluginCallError("not a share target")
            if not files or len(files) > surfaces.MAX_SHARE_FILES:
                raise PluginCallError("send between 1 and 64 files")
            return surfaces.result_json(self.send_files(target, files))

        self.run_async(work, reply, error)


class NotifyService(_ActionMixin):
    """Capability ``notify``: popups shown through BlueFerry's notification policy.

    ApiVersion 1.5: the popup waits in the plugin. The content-free signal
    ``NotifyChanged()`` tells BlueFerry to fetch it with
    ``GetPendingNotification()``, so nothing personal travels on the bus
    for other processes to read. Only the connection that currently owns
    the daemon's name :data:`~blueferry.plugin_api.HOST_BUS_NAME` may fetch;
    any other caller gets ``AccessDenied`` and the popup stays queued. The
    owner is followed with an asynchronous ``GetNameOwner`` and then
    ``NameOwnerChanged``; only the match rule for that signal (AddMatch) is
    a synchronous round trip, done once when the service is put on the bus,
    before :func:`run` starts the main loop. While the owner is unknown or
    the name has no owner, every fetch is refused. A service that is not on a bus (in-process
    tests, :class:`~blueferry.plugin_api.testing.FakeHost`) has no other
    callers and answers anyone.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Set before the base class runs: it calls add_to_connection when
        # it gets a bus. Unique name owning HOST_BUS_NAME: None until the
        # bus answered, "" while nobody owns it. Main loop only.
        self._host_owner: str | None = None
        self._host_watch: Any = None
        super().__init__(*args, **kwargs)
        self._pending: deque[tuple[float, surfaces.Notification]] = deque(
            maxlen=surfaces.MAX_PENDING_NOTIFICATIONS,
        )
        self._pending_lock = threading.Lock()

    def add_to_connection(self, connection: Any, path: Any) -> None:
        """Export the service and start following the daemon's name, also
        when the service was built without a bus and exported later."""
        super().add_to_connection(connection, path)
        if self._host_watch is None and hasattr(connection, "watch_name_owner"):
            # AddMatch for NameOwnerChanged is one synchronous round trip,
            # once per export (in run(): before the main loop starts);
            # GetNameOwner is asynchronous.
            # Its answer and every NameOwnerChanged arrive through the main
            # loop before any call the new owner can make (the bus keeps
            # the order).
            self._host_watch = connection.watch_name_owner(
                HOST_BUS_NAME, self._host_owner_changed,
            )

    def _host_owner_changed(self, owner: str) -> None:
        self._host_owner = str(owner)

    def remove_from_connection(self, *args: Any, **kwargs: Any) -> None:
        """Also stop following the daemon's name; nothing is fetched after."""
        if self._host_watch is not None:
            self._host_watch.cancel()
            self._host_watch = None
        self._host_owner = None
        super().remove_from_connection(*args, **kwargs)

    def _may_fetch(self, sender: str | None) -> bool:
        """Only BlueFerry's daemon takes popups out of the queue."""
        if getattr(self, "_connection", None) is None:
            return True  # not exported: only in-process callers exist
        owner = self._host_owner
        return bool(owner) and sender is not None and str(sender) == owner

    def emit_notify(
        self, title: str, body: str, icon: str = "", action_label: str = "",
        action_id: str = "",
    ) -> None:
        """Queue a popup and tell BlueFerry; a click on the button calls
        ``invoke_action("notify", action_id, {})``. Safe from any thread."""
        note = surfaces.parse_notification(title, body, icon, action_label, action_id)
        if note is None:
            return
        with self._pending_lock:
            self._pending.append((self._clock(), note))
        self._to_main(self.NotifyChanged)

    def _next_pending(self) -> tuple[surfaces.Notification | None, bool]:
        now = self._clock()
        with self._pending_lock:
            while self._pending:
                queued_at, note = self._pending.popleft()
                if now - queued_at <= surfaces.PENDING_NOTIFICATION_TTL_SEC:
                    return note, bool(self._pending)
            return None, False

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s", sender_keyword="sender",
    )
    def GetPendingNotification(self, sender=None) -> str:
        """The oldest waiting popup (removed from the queue), or null.

        Only the current owner of the BlueFerry daemon's bus name may call
        this; anyone else gets AccessDenied and the queue stays untouched.
        """
        self.admit(sender)
        if not self._may_fetch(sender):
            raise AccessDeniedError("only BlueFerry may fetch popups")
        note, more = self._next_pending()
        return surfaces.pending_json(note, more=more)

    @dbus.service.signal(PLUGIN_INTERFACE, signature="")
    def NotifyChanged(self) -> None:
        """A popup is waiting; BlueFerry calls GetPendingNotification. No content."""


def emit_card_changed(service: CardService) -> None:
    """Module-level form of :meth:`CardService.emit_card_changed`."""
    service.emit_card_changed()


def emit_notify(
    service: NotifyService, title: str, body: str, icon: str = "",
    action_label: str = "", action_id: str = "",
) -> None:
    """Module-level form of :meth:`NotifyService.emit_notify`."""
    service.emit_notify(title, body, icon, action_label, action_id)


def run(
    make_service: Callable[[Any], PluginService],
    *,
    idle_seconds: int = 600,
    log_file: bool = True,
) -> int:
    """Own the plugin's bus name and serve until idle; return an exit code.

    Logging goes to the plugin's standard file as well (see
    :mod:`blueferry.plugin_api.logs`), so a D-Bus activated plugin's log
    lines are not lost on a console nobody reads.
    """
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    service = make_service(bus)
    if log_file:
        logs.setup_logging(service.manifest.id)
        log.info("%s %s started", service.manifest.id, service.manifest.version)
    try:
        name = dbus.service.BusName(
            service.manifest.bus_name, bus, allow_replacement=False,
            replace_existing=False, do_not_queue=True,
        )
    except dbus.exceptions.NameExistsException:
        log.info("%s is already running", service.manifest.bus_name)
        return 0
    loop = GLib.MainLoop()

    def check_idle() -> bool:
        if service.in_flight == 0 and time.monotonic() - service.last_activity > idle_seconds:
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(30, check_idle)
    loop.run()
    del name
    return 0
