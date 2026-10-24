"""Test harness for plugins and their clients; no bus, no threads.

``inline_service(ServiceClass, manifest, ...)`` builds a service whose worker
and main-loop hand-offs run inline. ``ServiceTransport`` lets a
:class:`~blueferry.plugin_api.client.PluginClient` call that service
directly, through the same validation the real D-Bus transport gets.
``ScriptedTransport`` replays canned replies for client-only tests.
``FakeHost`` plays BlueFerry's side of the ApiVersion 1.2 surfaces (card,
share, notify; popups the 1.5 way) and the 1.3 settings helpers
(TestConfig, ConfigLogin) against a service, with the clients' own
validation.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import (
    CAPABILITY_SHARE,
    PHOTOS_INTERFACE,
    PLUGIN_INTERFACE,
    config_flow,
    surfaces,
)
from .client import ConfigResult, PluginClient, PluginError
from .manifest import PluginManifest, parse_manifest
from .service import PluginService

_INTERFACES = frozenset({PLUGIN_INTERFACE, PHOTOS_INTERFACE})


def manifest(
    plugin_id: str = "io.example.photos",
    *,
    capabilities: str = "photos;",
    api_version: int | str = 1,
    extra: str = "",
) -> PluginManifest:
    return parse_manifest(
        "[BlueFerry Plugin]\n"
        f"Id={plugin_id}\nName=Example\nVersion=1.0\nApiVersion={api_version}\n"
        f"MinBlueFerry=0.1\nCapabilities={capabilities}\n"
        "Homepage=https://example.org/plugin\nExec=example-plugin serve\n" + extra
    )


def inline_service(factory: Callable[..., PluginService], *args: Any, **kwargs: Any):
    kwargs.setdefault("to_main", lambda callback: callback())
    kwargs.setdefault("start_worker", lambda work: work())
    return factory(*args, **kwargs)


class ServiceTransport:
    """Calls a service object in-process; owner is the current user."""

    def __init__(self, service: PluginService, *, uid: int | None = None) -> None:
        self.service = service
        self.uid = os.getuid() if uid is None else uid
        self.calls: list[tuple[str, str, tuple]] = []

    def call(self, bus_name, interface, method, signature, args, timeout) -> object:
        import dbus.exceptions
        from dbus.service import _method_lookup

        self.calls.append((interface, method, tuple(args)))
        if interface not in _INTERFACES:
            raise PluginError("unknown interface")
        # The same lookup dbus-python does for a call on the bus: a member
        # on another interface than the one called is not found.
        try:
            function, _parent = _method_lookup(self.service, method, interface)
        except (AttributeError, dbus.exceptions.DBusException):
            raise PluginError("unknown method") from None
        options = getattr(function, "_dbus_async_callbacks", None)
        try:
            if options:
                outcome: dict[str, object] = {}
                function(
                    self.service, *args,
                    reply=lambda value: outcome.setdefault("reply", value),
                    error=lambda error: outcome.setdefault("error", error),
                    sender=":1.test",
                )
                if "error" in outcome:
                    raise PluginError(str(outcome["error"]))
                return outcome["reply"]
            return function(self.service, *args, sender=":1.test")
        except dbus.exceptions.DBusException as error:
            raise PluginError(str(error.get_dbus_message() or error)) from None

    def owner_uid(self, bus_name: str) -> int | None:
        return self.uid


class ScriptedTransport:
    """Canned replies keyed by method name; values may be callables."""

    def __init__(self, replies: Mapping[str, object], *, uid: int | None = None) -> None:
        self.replies = dict(replies)
        self.uid = os.getuid() if uid is None else uid
        self.calls: list[tuple[str, tuple]] = []

    def call(self, bus_name, interface, method, signature, args, timeout) -> object:
        self.calls.append((method, tuple(args)))
        reply = self.replies.get(method)
        if isinstance(reply, Exception):
            raise reply
        return reply(*args) if callable(reply) else reply

    def owner_uid(self, bus_name: str) -> int | None:
        return self.uid


class FakeHost:
    """BlueFerry's side of the 1.2 surfaces, in-process, for plugin tests.

    ``host = FakeHost(inline_service(MyPlugin, manifest))`` then
    ``host.card_items()``, ``host.invoke(item, action)``,
    ``host.share_targets()``, ``host.send(target, paths)`` and, for a card
    action with ``send_to`` (1.4), ``host.send_action(item, action, paths)``.
    Signals are
    captured instead of sent: ``host.card_changes`` counts CardChanged;
    on NotifyChanged (1.5) the host fetches with GetPendingNotification
    like BlueFerry does, ``host.notifications`` holds every validated popup and
    ``host.click(notification)`` presses its action button. Replies pass the
    same validation as in BlueFerry, so text is cut to the limits and an
    ``open_uri`` outside ``cache_root`` comes back as None.

    Settings (ApiVersion 1.3): ``host.get_config()``, ``host.set_config(v)``,
    ``host.test_config(values)`` ("Test connection") and
    ``host.sign_in(values)``, which runs ConfigLogin and then polls
    ConfigLoginStatus until a final state (``max_polls``, no sleeping) and
    records the opened sign-in URL in ``host.opened``. ``host.cancel_sign_in``
    sends ConfigLoginCancel.
    """

    def __init__(
        self, service: PluginService, *, cache_root: Path | None = None,
    ) -> None:
        self.service = service
        self.card_changes = 0
        self.notifications: list[surfaces.Notification] = []
        self.opened: list[str] = []
        self.transport = ServiceTransport(service)
        self.client = PluginClient(
            service.manifest, transport=self.transport,
            cache_root=(lambda: cache_root) if cache_root is not None else None,
        )
        # Instance attributes shadow the signal methods, so the service's
        # emit helpers land here instead of on a bus.
        if hasattr(service, "CardChanged"):
            service.CardChanged = self._card_changed
        if hasattr(service, "NotifyChanged"):
            service.NotifyChanged = self._notify_changed

    def _card_changed(self) -> None:
        self.card_changes += 1

    def _notify_changed(self) -> None:
        for _fetch in range(surfaces.MAX_PENDING_NOTIFICATIONS):
            note, more = self.client.pending_notification()
            if note is not None:
                self.notifications.append(note)
            if not more:
                return

    def card_items(self) -> list[surfaces.CardItem]:
        return self.client.card_items()

    def invoke(
        self, item_id: str, action_id: str, args: Mapping[str, object] | None = None,
    ) -> surfaces.ActionResult:
        return self.client.invoke_action(item_id, action_id, args)

    def share_targets(self) -> list[surfaces.ShareTarget]:
        return self.client.share_targets()

    def send(self, target_id: str, paths: list[str]) -> surfaces.SendResult:
        return self.client.send_files(target_id, [str(path) for path in paths])

    def send_action(
        self, item_id: str, action_id: str, paths: list[str],
    ) -> surfaces.SendResult:
        """The user chose a card action with ``send_to`` (ApiVersion 1.4)
        and picked ``paths``, or dropped them on the item (``action_id``
        None is not allowed: name the sending action).

        Like BlueFerry, this fetches the card, requires the action to name a
        share target of a plugin with the ``share`` capability, and calls
        ``SendFiles`` (never ``InvokeAction``).
        """
        if not self.service.manifest.has(CAPABILITY_SHARE):
            raise PluginError("a sending action needs the share capability")
        item = next((entry for entry in self.card_items() if entry.id == item_id), None)
        if item is None:
            raise PluginError(f"no card item {item_id!r}")
        action = next((entry for entry in item.actions if entry.id == action_id), None)
        if action is None or not action.send_to:
            raise PluginError(f"{item_id}:{action_id} does not send files")
        return self.send(action.send_to, paths)

    def click(self, notification: surfaces.Notification) -> surfaces.ActionResult:
        """The user pressed the popup's action button."""
        if not notification.has_action:
            raise PluginError("this popup has no action")
        return self.client.invoke_action(
            surfaces.NOTIFY_ITEM_ID, notification.action_id, {}, notify=True,
        )

    # ---- settings (ApiVersion 1.3) ------------------------------------------------

    def get_config(self) -> dict[str, object]:
        return self.client.get_config()

    def set_config(self, values: Mapping[str, object]) -> ConfigResult:
        return self.client.set_config(values)

    def test_config(self, values: Mapping[str, object]) -> config_flow.ConfigTestResult:
        """The user pressed "Test connection" with these typed values."""
        return self.client.test_config(values)

    def sign_in(
        self, values: Mapping[str, object] | None = None, *, max_polls: int = 50,
    ) -> config_flow.LoginStep:
        """The user pressed "Sign in with …": start, open, poll to the end.

        Returns the final step; a flow still pending after ``max_polls``
        comes back as ``pending``.
        """
        step = self.client.config_login(values or {})
        if step.state != "open":
            return step
        self.opened.append(step.open_uri)
        login_id = step.login_id
        for _poll in range(max_polls):
            status = self.client.config_login_status(login_id)
            if status.final:
                return status
        return config_flow.LoginStep("pending", login_id=login_id)

    def cancel_sign_in(self, login_id: str) -> None:
        self.client.config_login_cancel(login_id)
