"""Plugin popups (capability notify): sender checks, policy, rate limit, click.

The bus and the notification service are recording fakes; no session bus is
opened and no plugin runs.
"""
from __future__ import annotations

import json

import pytest

from blueferry import config
from blueferry.event_dispatcher import EventDispatcher
from blueferry.plugin_api.surfaces import Notification
from blueferry.plugin_api.testing import manifest
from blueferry.plugin_notify import (
    FETCHES_PER_MINUTE,
    MAX_PENDING_SENDERS,
    POPUPS_PER_MINUTE,
    PluginPopup,
    PluginPopups,
)
from blueferry.sinks.libnotify import LibnotifySink

CALENDAR = manifest("io.example.calendar", capabilities="notify;", api_version="1.5")
OTHER = manifest("io.example.other", capabilities="notify;", api_version="1.5")


class _Bus:
    def __init__(self, owners=None, uids=None) -> None:
        self.owners = owners or {}
        self.uids = uids or {}
        self.receivers = []
        self.calls = []
        self.default_uid = 1000
        # Popups each connection holds for GetPendingNotification.
        self.pending: dict[str, list] = {}

    def add_signal_receiver(self, callback, **kwargs):
        self.receivers.append((callback, kwargs))
        return self

    def remove(self) -> None:
        self.receivers.clear()

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply, error, timeout=-1.0):
        self.calls.append((bus_name, interface, method, tuple(args)))
        if method == "GetNameOwner":
            owner = self.owners.get(args[0])
            return reply(owner) if owner else error(RuntimeError("no owner"))
        if method == "GetConnectionUnixUser":
            return reply(self.uids.get(args[0], self.default_uid))
        if method == "InvokeAction":
            return self.invoke(reply, error)
        if method == "GetPendingNotification":
            queue = self.pending.get(bus_name, [])
            entry = queue.pop(0) if queue else None
            if isinstance(entry, str):
                return reply(entry)
            return reply(json.dumps({"notification": entry, "more": bool(queue)}))
        raise AssertionError(method)


class _DeferredBus(_Bus):
    """Keep selected replies pending, like a real asynchronous bus."""

    def __init__(self, *methods, **kwargs):
        super().__init__(**kwargs)
        self.deferred = set(methods)
        self.waiting = []

    def call_async(self, bus_name, path, interface, method, signature, args,
                   reply, error, timeout=-1.0):
        if method not in self.deferred:
            return super().call_async(bus_name, path, interface, method, signature, args,
                                      reply, error, timeout)
        self.calls.append((bus_name, interface, method, tuple(args)))
        self.waiting.append((method, reply, error))

    def answer(self, method, value):
        for index, (name, reply, _error) in enumerate(self.waiting):
            if name == method:
                self.waiting.pop(index)
                reply(value)
                return
        raise AssertionError(f"no pending {method}")


def _popups(bus, shown, **kwargs):
    kwargs.setdefault("disabled", frozenset)
    popups = PluginPopups(bus, show=shown.append, plugins=lambda: [OTHER, CALENDAR],
                          uid=1000, **kwargs)
    popups.start()
    return popups


_FIELDS = ("title", "body", "icon", "action_label", "action_id")


def _emit(bus, *args, sender=":1.7"):
    """The plugin queues a popup and emits the content-free NotifyChanged()."""
    if args:
        bus.pending.setdefault(sender, []).append(dict(zip(_FIELDS, args, strict=False)))
    callback, kwargs = bus.receivers[0]
    callback(**{kwargs["sender_keyword"]: sender})


def test_only_the_owner_of_an_enabled_notify_plugin_gets_a_popup() -> None:
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7", OTHER.bus_name: ":1.8"},
               uids={":1.7": 1000, ":1.9": 1000})
    shown: list[PluginPopup] = []
    _popups(bus, shown)
    assert bus.receivers[0][1]["dbus_interface"] == "io.weirdware.BlueFerry.Plugin1"
    assert bus.receivers[0][1]["signal_name"] == "NotifyChanged"

    _emit(bus, "Dentist", "10:00 \x1b[2J", "view-calendar", "Open", "open")
    _emit(bus, "Spoof", "from a stranger", "", "", "", sender=":1.9")
    _emit(bus, "", "", "", "", "")
    assert [(p.plugin_id, p.note.title, p.note.body) for p in shown] == [
        (CALENDAR.id, "Dentist", "10:00 [2J")]
    assert shown[0].note.has_action
    # The popup is fetched from the verified connection, never from the stranger.
    fetched = [call[0] for call in bus.calls if call[2] == "GetPendingNotification"]
    assert fetched and set(fetched) == {":1.7"}


def test_a_plugin_run_by_another_user_is_ignored() -> None:
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"}, uids={":1.7": 0})
    shown: list[PluginPopup] = []
    _popups(bus, shown)
    _emit(bus, "Hi", "there")
    assert shown == []


def test_each_plugin_gets_a_few_popups_a_minute() -> None:
    now = [0.0]
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    shown: list[PluginPopup] = []
    _popups(bus, shown, clock=lambda: now[0])
    for _ in range(POPUPS_PER_MINUTE + 3):
        _emit(bus, "Hi", "there")
    assert len(shown) == POPUPS_PER_MINUTE
    fetches = sum(1 for call in bus.calls if call[2] == "GetPendingNotification")
    assert fetches == POPUPS_PER_MINUTE  # over the limit nothing is even fetched
    # The rest waits in the plugin (which drops it after a minute); a
    # minute later the next signal fetches what is still there.
    now[0] = 61.0
    _emit(bus, "Later", "again")
    assert shown[-1].note.title == "Later" and len(shown) <= 2 * POPUPS_PER_MINUTE


def test_waiting_popups_are_fetched_one_after_another() -> None:
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    shown: list[PluginPopup] = []
    _popups(bus, shown)
    bus.pending[":1.7"] = [{"title": "One"}, {"title": "Two"}, {"title": "Three"}]
    _emit(bus)
    assert [p.note.title for p in shown] == ["One", "Two", "Three"]


@pytest.mark.parametrize("reply", [
    "{nope", "[]", json.dumps({"notification": 3}), json.dumps({"more": "yes"}),
    json.dumps({"notification": {"title": "x" * 600_000}}),
])
def test_a_broken_pending_reply_shows_nothing(reply) -> None:
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    shown: list[PluginPopup] = []
    _popups(bus, shown)
    bus.pending[":1.7"] = [reply]
    _emit(bus)
    assert shown == []


def test_a_verified_sender_gets_no_popup_after_it_was_disabled() -> None:
    disabled: set[str] = set()
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    shown: list[PluginPopup] = []
    popups = PluginPopups(bus, show=shown.append, plugins=lambda: [CALENDAR], uid=1000,
                          disabled=lambda: frozenset(disabled))
    popups.start()
    _emit(bus, "Before", "x")
    assert [p.note.title for p in shown] == ["Before"]
    # Disabling is a kill switch: no TTL, the very next signal is refused.
    disabled.add(CALENDAR.id)
    calls = len(bus.calls)
    _emit(bus, "After", "x")
    assert [p.note.title for p in shown] == ["Before"]
    assert len(bus.calls) == calls  # not even fetched


def test_a_click_invokes_the_plugin_and_opens_only_safe_uris(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    ics = tmp_path / "blueferry" / CALENDAR.id / "a.ics"
    ics.parent.mkdir(parents=True)
    ics.write_text("x")
    import os

    opened: list[str] = []
    bus = _Bus()
    bus.default_uid = os.getuid()
    popups = PluginPopups(bus, show=lambda _p: None, plugins=lambda: [CALENDAR],
                          uid=os.getuid(), open_uri=opened.append, disabled=frozenset)
    popup = PluginPopup(CALENDAR.id, "Calendar", CALENDAR.bus_name,
                        Notification("T", "B", "", "Open", "open"))
    for uri in (ics.as_uri(), "file:///etc/passwd", "https://example.org/e"):
        bus.invoke = lambda reply, error, uri=uri: reply(json.dumps({"ok": True,
                                                                      "open_uri": uri}))
        popups.invoke(popup)
    bus.invoke = lambda reply, error: reply("{garbage")
    popups.invoke(popup)
    bus.invoke = lambda reply, error: error(RuntimeError("crashed"))
    popups.invoke(popup)
    assert opened == [ics.resolve().as_uri(), "https://example.org/e"]
    invoke = next(call for call in bus.calls if call[2] == "InvokeAction")
    assert invoke == (CALENDAR.bus_name, "io.weirdware.BlueFerry.Plugin1", "InvokeAction",
                      ("notify", "open", "{}"))
    # A popup without a button never calls the plugin.
    count = len(bus.calls)
    popups.invoke(PluginPopup(CALENDAR.id, "Calendar", CALENDAR.bus_name, Notification("T", "B")))
    assert len(bus.calls) == count


@pytest.mark.parametrize("phase", ["click", "action-reply", "owner-reply"])
def test_disabling_blocks_old_popup_actions_and_late_results(phase) -> None:
    disabled = set()
    bus = _DeferredBus("InvokeAction", "GetConnectionUnixUser")
    opened = []
    popups = PluginPopups(bus, show=lambda _p: None, plugins=lambda: [CALENDAR],
                          uid=1000, open_uri=opened.append,
                          disabled=lambda: frozenset(disabled))
    popup = PluginPopup(CALENDAR.id, "Calendar", CALENDAR.bus_name,
                        Notification("T", "B", "", "Open", "open"))
    if phase == "click":
        disabled.add(CALENDAR.id)
    popups.invoke(popup)
    if phase == "click":
        assert bus.calls == []
    else:
        if phase == "action-reply":
            disabled.add(CALENDAR.id)
        bus.answer("InvokeAction", json.dumps({"ok": True, "open_uri": "https://example.org"}))
        if phase == "owner-reply":
            disabled.add(CALENDAR.id)
            bus.answer("GetConnectionUnixUser", 1000)
        else:
            assert [call[2] for call in bus.calls] == ["InvokeAction"]
    assert opened == []


def test_notification_bursts_share_pending_lookups_and_fetches() -> None:
    bus = _DeferredBus("GetNameOwner", "GetPendingNotification")
    shown = []
    popups = PluginPopups(bus, show=shown.append, plugins=lambda: [CALENDAR],
                          uid=1000, disabled=frozenset)
    popups.start()
    for _ in range(1000):
        _emit(bus)
    assert [call[2] for call in bus.calls] == ["GetNameOwner"]
    bus.answer("GetNameOwner", ":1.7")
    for _ in range(1000):
        _emit(bus)
    assert [entry[0] for entry in bus.waiting] == ["GetPendingNotification"]
    # A signal after the first request needs one follow-up, even if that
    # request's earlier snapshot said the queue was empty.
    bus.answer("GetPendingNotification", json.dumps({"notification": None, "more": False}))
    assert [entry[0] for entry in bus.waiting] == ["GetPendingNotification"]
    bus.answer("GetPendingNotification", json.dumps({"notification": {"title": "New"}, "more": False}))
    assert [popup.note.title for popup in shown] == ["New"]
    assert not bus.waiting and not popups._pending and not popups._fetch_again
    assert sum(call[2] == "GetPendingNotification" for call in bus.calls) == 2


def test_many_senders_cannot_start_unbounded_owner_lookups() -> None:
    bus = _DeferredBus("GetNameOwner")
    popups = PluginPopups(bus, show=lambda _p: None, plugins=lambda: [CALENDAR],
                          uid=1000, disabled=frozenset)
    popups.start()
    for n in range(1000):
        _emit(bus, sender=f":1.{n}")
    assert len(bus.waiting) == MAX_PENDING_SENDERS
    for _ in range(MAX_PENDING_SENDERS):
        bus.answer("GetNameOwner", ":2.1")
    assert not popups._pending
    _emit(bus, sender=":3.1")
    assert len(bus.waiting) == 1


def test_empty_fetches_also_have_a_rate_limit() -> None:
    now = [0.0]
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    popups = _popups(bus, [], clock=lambda: now[0])
    for _ in range(1000):
        _emit(bus)
    assert sum(call[2] == "GetPendingNotification" for call in bus.calls) == FETCHES_PER_MINUTE
    assert not popups._pending
    now[0] = 61.0
    _emit(bus)
    assert sum(call[2] == "GetPendingNotification" for call in bus.calls) == FETCHES_PER_MINUTE + 1


@pytest.mark.parametrize("outcome", ["invalid", "error", "disabled", "stopped"])
def test_pending_fetch_releases_its_slot_and_obeys_disable_or_stop(outcome) -> None:
    disabled = set()
    bus = _DeferredBus("GetPendingNotification", owners={CALENDAR.bus_name: ":1.7"})
    shown = []
    popups = _popups(bus, shown, disabled=lambda: frozenset(disabled))
    _emit(bus)
    if outcome == "invalid":
        bus.answer("GetPendingNotification", "invalid json")
    elif outcome == "error":
        _method, _reply, failure = bus.waiting.pop()
        failure(RuntimeError("timeout"))
    else:
        if outcome == "disabled":
            disabled.add(CALENDAR.id)
        else:
            popups.stop()
        bus.answer("GetPendingNotification", json.dumps({"notification": {"title": "Late"}, "more": True}))
    assert not popups._pending and not popups._fetch_again
    assert not shown and not bus.waiting
    disabled.clear()
    if outcome == "stopped":
        popups.start()
    _emit(bus)
    assert len(bus.waiting) == 1


def test_a_dispatch_exception_does_not_leave_a_sender_busy() -> None:
    class BrokenBus(_Bus):
        def call_async(self, *_args, **_kwargs):
            raise RuntimeError("bus disconnected")

    bus = BrokenBus()
    popups = _popups(bus, [])
    _emit(bus)
    assert not popups._pending


class _Notifications:
    def __init__(self) -> None:
        self.calls = []

    def Notify(self, *args, reply_handler=None, error_handler=None):
        # Plugin popups never block the daemon's loop.
        assert reply_handler is not None and error_handler is not None
        self.calls.append(args)
        reply_handler(50 + len(self.calls))


def _sink(policy="messages", on_plugin_action=None):
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: policy
    sink._notif = _Notifications()
    sink._pending = {}
    sink._msg_subs = {}
    sink._plugin_popups = {}
    sink._on_plugin_action = on_plugin_action
    return sink


def _popup(**note) -> PluginPopup:
    values = {"title": "<b>Dentist</b>", "body": "10:00 & more", "icon": "view-calendar",
              "action_label": "Open <i>now</i>", "action_id": "open"}
    values.update(note)
    return PluginPopup(CALENDAR.id, "Calendar", CALENDAR.bus_name, Notification(**values))


@pytest.mark.parametrize("content", [True, False])
def test_sink_escapes_text_and_honours_the_content_switch(monkeypatch, content) -> None:
    monkeypatch.setattr(config, "SHOW_NOTIFICATION_CONTENT", content)
    clicked = []
    sink = _sink(on_plugin_action=clicked.append)
    sink.handle_plugin_notification(_popup())
    _app, _replace, icon, title, body, actions, _hints, _timeout = sink._notif.calls[0]
    assert icon == "view-calendar"
    if content:
        assert title == "&lt;b&gt;Dentist&lt;/b&gt;" and body == "10:00 &amp; more"
    else:
        assert title == "Calendar" and body == "New notification"
    assert list(actions) == ["plugin-action", "Open now" if content else "Open"]
    sink._on_action(51, "plugin-action")
    sink._on_action(51, "plugin-action")  # single use
    assert len(clicked) == 1 and clicked[0].note.action_id == "open"


def test_sink_respects_none_and_closing_never_runs_the_action() -> None:
    silent = _sink(policy="none", on_plugin_action=lambda _p: None)
    silent.handle_plugin_notification(_popup())
    assert silent._notif.calls == []
    clicked = []
    sink = _sink(on_plugin_action=clicked.append)
    sink.handle_plugin_notification(_popup())
    sink.handle_plugin_notification(_popup(action_label="", action_id=""))
    assert list(sink._notif.calls[1][5]) == []
    sink._on_closed(51, 2)
    sink._on_action(51, "plugin-action")
    assert clicked == []


def test_dispatcher_forwards_plugin_popups_and_the_click_hook() -> None:
    seen = []
    kwargs_seen = []

    class _Sink:
        name = "libnotify"

        def __init__(self, **kwargs) -> None:
            kwargs_seen.append(kwargs)

        def handle_plugin_notification(self, popup) -> None:
            seen.append(popup)

    class _NoBus:
        def add_signal_receiver(self, *_args, **_kwargs):
            return self

        def name_has_owner(self, _name):
            return True

    dispatcher = EventDispatcher(
        contacts=None, defer_mark_read=lambda _path: None,
        plugin_action=lambda _popup: None, notification_sink_factory=_Sink,
        otp_autocopy=lambda: False, session_bus=_NoBus(), storage=object(),
    )
    dispatcher._setup_complete = True
    assert dispatcher._ensure_libnotify_sink()
    assert "on_plugin_action" in kwargs_seen[0]
    dispatcher.plugin_notification(_popup())
    assert len(seen) == 1


def test_unverified_senders_are_rate_limited_and_verified_ones_remembered() -> None:
    now = [0.0]
    bus = _Bus(owners={CALENDAR.bus_name: ":1.7"})
    shown: list[PluginPopup] = []
    reads = []

    def plugins():
        reads.append(1)
        return [CALENDAR]

    popups = PluginPopups(bus, show=shown.append, plugins=plugins, uid=1000,
                          clock=lambda: now[0], disabled=frozenset)
    popups.start()
    for _ in range(40):
        _emit(bus, "Spam", "x", sender=":1.66")
    lookups = [call for call in bus.calls if call[2] == "GetNameOwner"]
    assert len(lookups) == 12  # LOOKUPS_PER_MINUTE, then dropped unasked
    assert len(reads) == 1  # manifests cached
    _emit(bus, "Hi", "there")

    def lookups_made() -> int:
        return sum(1 for call in bus.calls if call[0] == "org.freedesktop.DBus")

    asked = lookups_made()
    # The plugin idles out; its next popup still counts as verified.
    bus.owners.clear()
    _emit(bus, "Again", "later")
    assert [p.note.title for p in shown] == ["Hi", "Again"]
    assert lookups_made() == asked


def test_one_shot_senders_do_not_grow_the_lookup_table() -> None:
    now = [0.0]
    bus = _Bus()
    popups = PluginPopups(bus, show=lambda _p: None, plugins=lambda: [CALENDAR], uid=1000,
                          clock=lambda: now[0], disabled=frozenset)
    popups.start()
    for n in range(300):
        _emit(bus, "x", "y", sender=f":1.{n}")
    now[0] = 120.0
    for n in range(300, 600):
        _emit(bus, "x", "y", sender=f":1.{n}")
    assert len(popups._lookups) <= 300


def test_daemon_wires_plugin_popups_to_the_sinks_and_back(make_daemon, monkeypatch) -> None:
    from blueferry import daemon as daemon_mod

    instance = make_daemon()
    assert instance.events.plugin_action == instance._plugin_popup_action
    started: list[object] = []

    class Popups:
        def __init__(self, bus, *, show) -> None:
            self.show, self.invoked = show, []

        def start(self) -> None:
            started.append(self)

        def invoke(self, popup) -> None:
            self.invoked.append(popup)

        def stop(self) -> None:
            started.remove(self)

    monkeypatch.setattr(daemon_mod, "get_session_bus", lambda: object())
    monkeypatch.setattr(daemon_mod, "PluginPopups", Popups)
    instance._start_plugin_popups()
    instance._start_plugin_popups()  # idempotent
    (popups,) = started
    assert popups.show == instance.events.plugin_notification
    instance._plugin_popup_action("popup")
    assert popups.invoked == ["popup"]
    instance.plugin_popups.stop()
    instance.plugin_popups = None


def _run_until(condition, seconds: float = 5.0) -> None:
    import time

    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)


@pytest.mark.private_dbus
@pytest.mark.parametrize("owns_daemon_name", [True, False])
def test_on_the_bus_the_daemon_fetches_and_a_stranger_cannot(owns_daemon_name) -> None:
    """End to end on the private bus: a real NotifyService and PluginPopups.

    The plugin hands its popup only to the owner of io.weirdware.BlueFerry;
    the daemon fetches from the connection that owns that name.
    """
    import os

    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop

    from blueferry.plugin_api import HOST_BUS_NAME
    from blueferry.plugin_api.service import NotifyService
    from blueferry.protocol import BUS_NAME
    from tests.private_bus import open_private_bus

    assert HOST_BUS_NAME == BUS_NAME
    plugin = manifest(f"io.example.e{os.getpid()}{int(owns_daemon_name)}",
                      capabilities="notify;", api_version="1.5")
    plugin_bus = open_private_bus(mainloop=DBusGMainLoop())
    daemon_bus = open_private_bus(mainloop=DBusGMainLoop())
    plugin_bus.request_name(plugin.bus_name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    if owns_daemon_name:
        daemon_bus.request_name(BUS_NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    service = NotifyService(plugin, plugin_bus, start_worker=lambda work: work())
    shown: list[PluginPopup] = []
    popups = PluginPopups(daemon_bus, show=shown.append, plugins=lambda: [plugin],
                          disabled=frozenset, open_uri=lambda _uri: None)
    popups.start()
    try:
        # Let the plugin learn the daemon name's owner (or that it has none).
        _run_until(lambda: service._host_owner is not None)
        service.emit_notify("Reminder", "Dentist")
        _run_until(lambda: bool(shown) or not service._pending, 3.0)
        _run_until(lambda: False, 0.2)  # any late fetch has had its chance
        if owns_daemon_name:
            assert [popup.note.title for popup in shown] == ["Reminder"]
            assert not service._pending
        else:
            assert shown == []
            assert len(service._pending) == 1  # still waiting for BlueFerry
    finally:
        popups.stop()
        service.remove_from_connection()
        if owns_daemon_name:
            daemon_bus.release_name(BUS_NAME)
        plugin_bus.release_name(plugin.bus_name)
        daemon_bus.close()
        plugin_bus.close()
