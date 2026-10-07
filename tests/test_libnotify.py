"""Desktop popup policy tests."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from blueferry.sinks import libnotify as libnotify_mod
from blueferry.sinks.libnotify import (
    _ANCS_EXPIRE_MS,
    _MESSAGE_EXPIRE_MS,
    LibnotifySink,
)


@pytest.fixture(autouse=True)
def activation_clients(monkeypatch, tmp_path):
    monkeypatch.setattr(libnotify_mod, "get_session_bus", lambda: SimpleNamespace(list_names=lambda: []))
    monkeypatch.setattr("blueferry.client_activation.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("blueferry.client_activation.os.access", lambda *_args: False)


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls = []

    def Notify(self, *args):
        self.calls.append(args)
        return 1

    def CloseNotification(self, notification_id):
        self.calls.append(("close", int(notification_id)))


class _Match:
    def __init__(self) -> None:
        self.removed = False

    def remove(self) -> None:
        self.removed = True


def test_ancs_popup_is_transient_and_expires(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        app_name="Settings",
        app_id="com.apple.Preferences",
        title="System message",
        body="Something happened",
    )

    sink.handle_ancs(event)

    assert len(sink._notif.calls) == 1
    assert bool(sink._notif.calls[0][-2]["transient"]) is True
    assert int(sink._notif.calls[0][-1]) == _ANCS_EXPIRE_MS


def test_sms_and_imessage_popup_also_expires(monkeypatch) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        display_sender="Alice",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert len(sink._notif.calls) == 1
    assert list(sink._notif.calls[0][5]) == []
    assert int(sink._notif.calls[0][-1]) == _MESSAGE_EXPIRE_MS


def test_clicking_message_popup_requests_opaque_message_handle(monkeypatch) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    opened = []
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._open_messages = {}
    sink._msg_subs = {}
    sink._on_open_message = lambda handle, token: opened.append((handle, token))
    event = SimpleNamespace(
        kind="sms_received",
        handle="message-opaque-42",
        display_sender="Alice",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert list(sink._notif.calls[0][5]) == ["default", "Open conversation"]
    sink._on_action(1, "default")
    assert opened == [("message-opaque-42", "")]

    sink._on_closed(1, 1)
    sink._on_action(1, "default")
    assert opened == [("message-opaque-42", "")]


def test_message_popup_includes_omarchy_open_argv(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    monkeypatch.setattr(
        "blueferry.client_activation.os.access", lambda path, _mode: path.endswith("quickshell")
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._open_messages = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        handle="message-opaque-42",
        display_sender="Alice",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    hints = sink._notif.calls[0][-2]
    assert hints["desktop-entry"] == "io.weirdware.BlueFerry.Quickshell"
    assert json.loads(str(hints["omarchy-exec-argv"])) == [
        sys.executable, "-m", "blueferry.client_activation",
        "--message=message-opaque-42",
    ]


def test_remote_markup_is_escaped_before_notification(monkeypatch) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        display_sender="Alice & Bob",
        body="<b>not markup</b> & text",
        message_path=None,
    )

    sink.handle(event)

    call = sink._notif.calls[0]
    assert call[3] == "💬 Alice &amp; Bob"
    assert call[4] == "&lt;b&gt;not markup&lt;/b&gt; &amp; text"


def test_remote_notification_title_cannot_embed_controls_or_newlines(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        display_sender="Alice\nFake app\u202egnp",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert sink._notif.calls[0][3] == "💬 Alice Fake app�gnp"


def test_messages_ancs_duplicate_is_suppressed_by_default(monkeypatch) -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        app_name="Messages",
        app_id="com.apple.MobileSMS",
        title="Alice",
        body="hello",
    )

    sink.handle_ancs(event)

    assert sink._notif.calls == []


def test_messages_only_suppresses_non_message_ancs_popup() -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "messages"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        app_name="Settings",
        app_id="com.apple.Preferences",
        title="System message",
        body="Something happened",
    )

    sink.handle_ancs(event)

    assert sink._notif.calls == []


def test_none_suppresses_message_popup() -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "none"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        kind="sms_received",
        display_sender="Alice",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert sink._notif.calls == []


def test_contacts_only_suppresses_unknown_sender_popup() -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "messages"
    sink._contacts_only_notifications = lambda: True
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        kind="sms_received",
        contact_name=None,
        display_sender="+15551234567",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert sink._notif.calls == []


def test_contacts_only_allows_resolved_contact_popup() -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "messages"
    sink._contacts_only_notifications = lambda: True
    sink._notif = _FakeNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        contact_name="Alice",
        display_sender="Alice",
        body="Hello",
        message_path=None,
    )

    sink.handle(event)

    assert len(sink._notif.calls) == 1


def test_read_state_trackers_are_bounded(monkeypatch) -> None:
    removed = []
    subscription = SimpleNamespace(remove=lambda: removed.append(True))
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _FakeNotifications()
    sink._pending = {1: "/message/1", 2: "/message/2"}
    sink._msg_subs = {1: subscription, 2: subscription}
    monkeypatch.setattr(libnotify_mod, "MAX_DESKTOP_MESSAGE_TRACKERS", 1)

    sink._prune_trackers()

    assert sink._pending == {2: "/message/2"}
    assert set(sink._msg_subs) == {2}
    assert removed == [True]
    assert sink._notif.calls == [("close", 1)]


def test_deliberate_dismissal_defers_the_correct_mark_read(monkeypatch) -> None:
    deferred = []
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._pending = {7: "/session/message1"}
    sink._msg_subs = {}
    sink._defer_mark_read = deferred.append
    monkeypatch.setattr(libnotify_mod.config, "MARK_READ_ON_DISMISS", True)

    sink._on_closed(7, 2)

    assert sink._pending == {}
    assert deferred == ["/session/message1"]


def test_mark_read_on_dismiss_disabled_skips_the_write(monkeypatch) -> None:
    deferred = []
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._pending = {7: "/session/message1"}
    sink._msg_subs = {}
    sink._defer_mark_read = deferred.append
    monkeypatch.setattr(libnotify_mod.config, "MARK_READ_ON_DISMISS", False)

    sink._on_closed(7, 2)

    assert sink._pending == {}
    assert deferred == []


@pytest.mark.parametrize("reason", [1, 3])
def test_expiry_and_phone_read_do_not_write_read_state(reason) -> None:
    queued = []
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._pending = {7: "/session/message1"}
    sink._msg_subs = {}
    sink._defer_mark_read = queued.append

    sink._on_closed(7, reason)

    assert queued == []


def test_close_releases_all_signal_watches_and_trackers() -> None:
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._match = _Match()
    sink._action_match = _Match()
    sink._token_match = _Match()
    token_match = sink._token_match
    sink._activation_tokens = {7: "single-use"}
    message_match = _Match()
    sink._msg_subs = {7: message_match}
    sink._pending = {7: "/session/message1"}
    sink._open_messages = {7: "opaque-handle"}

    owner_match = sink._match
    action_match = sink._action_match
    sink.close()

    assert owner_match.removed is True
    assert action_match.removed is True
    assert token_match.removed is True
    assert sink._activation_tokens == {}
    assert message_match.removed is True
    assert sink._match is None
    assert sink._action_match is None
    assert sink._msg_subs == {}
    assert sink._pending == {}
    assert sink._open_messages == {}


def test_tokens_are_scoped_to_notification_and_consumed_once(monkeypatch):
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._open_messages = {1: "first", 2: "second"}
    sink._activation_tokens = {}
    sink._pending = {}
    sink._msg_subs = {}
    opened = []
    sink._on_open_message = lambda *args: opened.append(args)
    sink._on_activation_token(999, "unrelated")
    sink._on_activation_token(1, "gnome-token")
    sink._on_action(2, "default")
    sink._on_action(1, "default")
    sink._on_action(1, "default")
    assert opened == [("second", ""), ("first", "gnome-token"), ("first", "")]
    sink._on_activation_token(1, "unused")
    sink._on_closed(1, 1)
    assert sink._activation_tokens == {}


# ---- opt-in ANCS notification actions ------------------------------------

class _NotificationObject:
    """Stands in for org.freedesktop.Notifications behind dbus.Interface."""

    def __init__(self, capabilities=("actions", "body")) -> None:
        self.calls = []
        self.signals = {}
        self.next_id = 100
        self.capabilities = list(capabilities)

    def get_dbus_method(self, member, _interface=None):
        return getattr(self, f"_{member}")

    def connect_to_signal(self, name, handler, *_args, **_kwargs):
        self.signals[name] = handler
        return _Match()

    def _Notify(self, *args):
        self.next_id += 1
        self.calls.append(("notify", self.next_id, args))
        return self.next_id

    def _GetCapabilities(self, **kwargs):
        # Queried asynchronously; never a blocking call at sink start.
        kwargs["reply_handler"](self.capabilities)

    def _CloseNotification(self, notification_id, **kwargs):
        # The action-popup path must close asynchronously.
        assert "reply_handler" in kwargs and "error_handler" in kwargs
        self.calls.append(("close", int(notification_id)))
        kwargs["reply_handler"]()


def _action_sink(
    monkeypatch, *, enabled: bool, callback=None, policy="all",
    capabilities=("actions", "body"),
):
    server = _NotificationObject(capabilities)
    bus = SimpleNamespace(
        get_object=lambda _name, _path: server,
        list_names=lambda: [],
    )
    monkeypatch.setattr(libnotify_mod, "get_session_bus", lambda: bus)
    monkeypatch.setattr(libnotify_mod.config, "SHOW_NOTIFICATION_CONTENT", True)
    sink = LibnotifySink(
        defer_mark_read=lambda _path: None,
        notification_policy=lambda: policy,
        on_ancs_action=callback,
        # Mirrors the daemon: the saved opt-in and visible content.
        ancs_actions_enabled=lambda: (
            enabled and libnotify_mod.config.SHOW_NOTIFICATION_CONTENT
        ),
    )
    return sink, server


def _call_event(**overrides):
    from blueferry.ancs.events import AncsEvent

    values = dict(
        notification_id=42,
        app_id="com.apple.mobilephone",
        app_name="Phone",
        title="Alice",
        subtitle="",
        body="Incoming call",
        positive_action_label="Accept",
        negative_action_label="Decline",
        action_token=5,
    )
    values.update(overrides)
    return AncsEvent(**values)


def _notify_calls(server):
    return [call for call in server.calls if call[0] == "notify"]


def test_disabled_actions_leave_ancs_popup_unchanged(monkeypatch) -> None:
    invoked = []
    disabled, disabled_server = _action_sink(
        monkeypatch, enabled=False, callback=lambda *args: invoked.append(args)
    )
    disabled.handle_ancs(_call_event())
    baseline, baseline_server = _action_sink(monkeypatch, enabled=False)
    baseline.handle_ancs(_call_event(
        positive_action_label="", negative_action_label=""
    ))

    (_, nid, args), = _notify_calls(disabled_server)
    (_, _, expected), = _notify_calls(baseline_server)
    assert list(args[5]) == []
    assert args == expected
    disabled_server.signals["ActionInvoked"](nid, "ancs-positive")
    assert invoked == []


def test_enabled_actions_add_labelled_buttons_and_a_no_op_default(monkeypatch) -> None:
    invoked = []
    sink, server = _action_sink(
        monkeypatch, enabled=True, callback=lambda *args: invoked.append(args),
    )

    sink.handle_ancs(_call_event())

    (_, nid, args), = _notify_calls(server)
    # The explicit default keeps servers from mapping a body click onto the
    # only (or first) action; for ANCS popups it does nothing.
    assert list(args[5]) == [
        "default", "", "ancs-positive", "Accept", "ancs-negative", "Decline",
    ]
    assert bool(args[6]["transient"]) is True
    server.signals["ActionInvoked"](nid, "default")
    assert invoked == []


def test_server_without_action_support_gets_no_buttons(monkeypatch) -> None:
    sink, server = _action_sink(
        monkeypatch, enabled=True, callback=lambda *a: True, capabilities=("body",),
    )

    sink.handle_ancs(_call_event())

    (_, _nid, args), = _notify_calls(server)
    assert list(args[5]) == []
    assert int(args[7]) == libnotify_mod._ANCS_EXPIRE_MS
    assert sink._ancs_actions == {}


def test_lone_negative_action_is_never_the_body_click(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(positive_action_label="", negative_action_label="Clear"))

    (_, _nid, args), = _notify_calls(server)
    assert list(args[5])[:2] == ["default", ""]
    assert list(args[5])[2:] == ["ancs-negative", "Clear"]


def test_only_offered_actions_become_buttons(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(positive_action_label=""))
    sink.handle_ancs(_call_event(
        notification_id=43, positive_action_label="", negative_action_label="",
    ))

    first, second = _notify_calls(server)
    assert list(first[2][5]) == ["default", "", "ancs-negative", "Decline"]
    assert list(second[2][5]) == []
    assert sink._ancs_actions == {first[1]: (42, 5)}


def test_action_labels_are_sanitized_for_display(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(positive_action_label="Ac‮cept"))

    (_, _nid, args), = _notify_calls(server)
    assert "‮" not in args[5][3]


def test_click_invokes_the_matching_action_exactly_once(monkeypatch) -> None:
    invoked = []
    sink, server = _action_sink(
        monkeypatch, enabled=True,
        callback=lambda uid, positive, token, _done: invoked.append(
            (uid, positive, token)
        ),
    )
    sink.handle_ancs(_call_event())
    sink.handle_ancs(_call_event(notification_id=77, action_token=6))
    (_, first, _), (_, second, _) = _notify_calls(server)

    server.signals["ActionInvoked"](first, "ancs-negative")
    server.signals["ActionInvoked"](first, "ancs-negative")
    server.signals["ActionInvoked"](first, "ancs-positive")
    server.signals["ActionInvoked"](second, "ancs-positive")
    server.signals["ActionInvoked"](9999, "ancs-positive")

    assert invoked == [(42, False, 5), (77, True, 6)]


def test_dismiss_expiry_and_body_click_never_invoke_actions(monkeypatch) -> None:
    invoked = []
    sink, server = _action_sink(
        monkeypatch, enabled=True, callback=lambda *args: invoked.append(args),
    )
    for uid in (1, 2, 3):
        sink.handle_ancs(_call_event(notification_id=uid))
    nids = [call[1] for call in _notify_calls(server)]

    server.signals["NotificationClosed"](nids[0], 2)   # dismissed
    server.signals["NotificationClosed"](nids[1], 1)   # expired
    server.signals["ActionInvoked"](nids[2], "default")
    server.signals["ActionInvoked"](nids[0], "ancs-positive")
    server.signals["ActionInvoked"](nids[1], "ancs-positive")

    assert invoked == []
    assert list(sink._ancs_actions.values()) == [(3, 5)]


def test_failed_action_shows_content_free_feedback(monkeypatch) -> None:
    def perform(_uid, _positive, _token, done):
        done("unavailable")

    sink, server = _action_sink(monkeypatch, enabled=True, callback=perform)
    sink.handle_ancs(_call_event(title="Private caller"))
    nid = _notify_calls(server)[0][1]

    server.signals["ActionInvoked"](nid, "ancs-positive")

    feedback = _notify_calls(server)[1][2]
    assert "no longer available" in feedback[4]
    assert "Private caller" not in feedback[3] + feedback[4]
    assert list(feedback[5]) == []


def test_successful_action_needs_no_feedback(monkeypatch) -> None:
    sink, server = _action_sink(
        monkeypatch, enabled=True,
        callback=lambda _uid, _positive, _token, done: done("sent"),
    )
    sink.handle_ancs(_call_event())
    server.signals["ActionInvoked"](_notify_calls(server)[0][1], "ancs-positive")

    assert len(_notify_calls(server)) == 1


def test_phone_removal_closes_the_actionable_popup(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)
    sink.handle_ancs(_call_event())
    nid = _notify_calls(server)[0][1]

    sink.close_ancs_notification(999)
    sink.close_ancs_notification(42)

    assert ("close", nid) in server.calls
    assert sink._ancs_actions == {}


def test_messages_ancs_stays_suppressed_with_actions(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(app_id="com.apple.MobileSMS"))

    assert _notify_calls(server) == []


def test_messages_policy_shows_no_ancs_actions(monkeypatch) -> None:
    sink, server = _action_sink(
        monkeypatch, enabled=True, callback=lambda *a: True, policy="messages",
    )

    sink.handle_ancs(_call_event())

    assert _notify_calls(server) == []


def test_close_forgets_actionable_popups_without_closing_foreign_ids(monkeypatch) -> None:
    # close() runs when the notification server's owner changed: the old
    # popups died with it and their ids may now belong to another app.
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)
    sink.handle_ancs(_call_event())

    sink.close()

    assert sink._ancs_actions == {}
    assert [call for call in server.calls if call[0] == "close"] == []


def test_hidden_notification_content_also_hides_action_labels(monkeypatch) -> None:
    invoked = []
    sink, server = _action_sink(
        monkeypatch, enabled=True, callback=lambda *args: invoked.append(args),
    )
    monkeypatch.setattr(libnotify_mod.config, "SHOW_NOTIFICATION_CONTENT", False)

    sink.handle_ancs(_call_event(positive_action_label="Pay CHF 50 to Bob"))
    (_, nid, args), = _notify_calls(server)
    server.signals["ActionInvoked"](nid, "ancs-positive")

    assert list(args[5]) == []
    assert "Bob" not in repr(args)
    assert int(args[7]) == libnotify_mod._ANCS_EXPIRE_MS
    assert invoked == []


def test_markup_is_removed_from_action_labels(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(
        positive_action_label="<b>Accept</b>", negative_action_label="Tom & Jerry",
    ))

    (_, _nid, args), = _notify_calls(server)
    assert list(args[5]) == [
        "default", "", "ancs-positive", "Accept", "ancs-negative", "Tom Jerry",
    ]


@pytest.mark.parametrize(
    "label,shown",
    [
        ("Reply <3 > now", "Reply 3 now"),
        ("<i>Mark</i> read", "Mark read"),
        ('<a href="x">Open</a>', "Open"),
        ("a < b > c", "a b c"),
    ],
)
def test_only_real_tags_are_removed_from_labels(monkeypatch, label, shown) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(positive_action_label=label, negative_action_label=""))

    (_, _nid, args), = _notify_calls(server)
    assert list(args[5])[2:] == ["ancs-positive", shown]


def test_markup_only_label_offers_no_button(monkeypatch) -> None:
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event(positive_action_label="<>", negative_action_label=""))

    (_, _nid, args), = _notify_calls(server)
    assert list(args[5]) == []


def test_action_popups_use_the_longer_action_timeout(monkeypatch) -> None:
    monkeypatch.setattr(libnotify_mod, "_ANCS_ACTION_EXPIRE_MS", 30_000)
    monkeypatch.setattr(libnotify_mod, "_ANCS_EXPIRE_MS", 8_000)
    sink, server = _action_sink(monkeypatch, enabled=True, callback=lambda *a: True)

    sink.handle_ancs(_call_event())
    sink.handle_ancs(_call_event(
        notification_id=43, positive_action_label="", negative_action_label="",
    ))

    with_actions, without_actions = _notify_calls(server)
    assert int(with_actions[2][7]) == 30_000
    assert int(without_actions[2][7]) == 8_000


def test_session_reset_retires_every_action_popup(monkeypatch) -> None:
    invoked = []
    sink, server = _action_sink(
        monkeypatch, enabled=True,
        callback=lambda uid, positive, _token, _done: invoked.append((uid, positive)),
    )
    sink.handle_ancs(_call_event())
    sink.handle_ancs(_call_event(notification_id=43))
    old = [call[1] for call in _notify_calls(server)]

    sink.close_all_ancs_notifications()
    # The next session reuses UID 42 for an unrelated notification.
    sink.handle_ancs(_call_event(positive_action_label="Delete"))
    for nid in old:
        server.signals["ActionInvoked"](nid, "ancs-positive")

    assert [("close", nid) for nid in old] == [
        call for call in server.calls if call[0] == "close"
    ]
    assert invoked == []
    assert list(sink._ancs_actions.values()) == [(42, 5)]


@pytest.mark.parametrize("result", ["busy", "failed"])
def test_retryable_failure_offers_a_retry_button(monkeypatch, result) -> None:
    calls = []

    def perform(uid, positive, token, done):
        calls.append((uid, positive, token))
        done(result if len(calls) == 1 else "sent")

    sink, server = _action_sink(monkeypatch, enabled=True, callback=perform)
    sink.handle_ancs(_call_event(title="Private caller"))
    server.signals["ActionInvoked"](_notify_calls(server)[0][1], "ancs-negative")

    (_, feedback_nid, feedback), = _notify_calls(server)[1:]
    assert list(feedback[5]) == ["default", "", "ancs-retry", "Retry"]
    assert "Private caller" not in repr(feedback)

    server.signals["ActionInvoked"](feedback_nid, "ancs-retry")
    server.signals["ActionInvoked"](feedback_nid, "ancs-retry")
    assert calls == [(42, False, 5), (42, False, 5)]
    assert sink._ancs_retries == {}


def test_phone_removal_also_closes_its_retry_popup(monkeypatch) -> None:
    sink, server = _action_sink(
        monkeypatch, enabled=True,
        callback=lambda _uid, _positive, _token, done: done("busy"),
    )
    sink.handle_ancs(_call_event())
    server.signals["ActionInvoked"](_notify_calls(server)[0][1], "ancs-positive")
    retry_nid = _notify_calls(server)[1][1]

    sink.close_ancs_notification(42)

    assert ("close", retry_nid) in server.calls
    assert sink._ancs_retries == {}
