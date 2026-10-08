"""Desktop popup policy tests."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from blueferry.ancs.constants import ANCS_MESSAGE_MAX_BYTES, ANCS_SUBTITLE_MAX_BYTES
from blueferry.notification_open_map import OpenTarget, resolve_open_target
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
        subtitle="",
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


class _AsyncFakeNotifications(_FakeNotifications):
    def Notify(self, *args, reply_handler=None, error_handler=None):
        self.calls.append(args)
        if reply_handler is not None:
            reply_handler(args[1])
            return None
        return 7


def test_another_sink_can_extend_an_open_message_popup(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notif = _AsyncFakeNotifications()
    sink._pending = {}
    sink._open_messages = {}
    sink._msg_subs = {}
    event = SimpleNamespace(
        kind="sms_received",
        handle="message-7",
        display_sender="+15551234567",
        body="Your code is <b>482913</b>",
        message_path=None,
    )
    sink.handle(event)

    assert sink.amend_message_popup("message-7", "Code copied <now>.")

    original, amended = sink._notif.calls
    # Same popup id, actions and hints; the line is escaped and appended.
    assert int(amended[1]) == 7
    assert amended[3] == original[3]
    assert amended[4] == f"{original[4]}\nCode copied &lt;now&gt;."
    assert list(amended[5]) == list(original[5])
    assert dict(amended[6]) == dict(original[6])
    # Unknown or closed popups are left to the caller.
    assert not sink.amend_message_popup("message-other", "x")
    sink._on_closed(7, 1)
    assert not sink.amend_message_popup("message-7", "x")
    assert not sink.amend_message_popup("", "x")
# ---- per-app notification click rules --------------------------------------

_HOSTILE_TITLE = "$(touch /tmp/pwned)`id`; rm -rf ~ && https://evil.example/"
_HOSTILE_BODY = "javascript:alert(1) file:///etc/passwd org.evil.App.desktop %u {body}"


class _CountingNotifications(_FakeNotifications):
    def __init__(self) -> None:
        super().__init__()
        self.next_id = 40

    def Notify(self, *args):
        self.calls.append(args)
        self.next_id += 1
        return self.next_id


def _clickable_sink(rules, opened, monkeypatch):
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _CountingNotifications()
    sink._pending = {}
    sink._msg_subs = {}
    sink._open_messages = {}
    sink._open_apps = {}
    sink._click_ids = {}
    sink._dismissed_clicks = {}
    sink._activation_tokens = {}
    sink._recent_open_targets = {}
    sink._open_target = lambda app_id: resolve_open_target(rules, app_id)
    sink._on_open_target = lambda target, token: opened.append((target, token))
    sink._on_open_message = lambda *_args: pytest.fail("not a message popup")
    return sink


def _ancs(app_id, title=_HOSTILE_TITLE, body=_HOSTILE_BODY):
    return SimpleNamespace(
        app_name="App", app_id=app_id, title=title, subtitle="", body=body,
    )


def test_mapped_app_popup_opens_only_the_configured_target(monkeypatch) -> None:
    opened = []
    rules = {"com.apple.mobilemail": "org.mozilla.Thunderbird.desktop"}
    sink = _clickable_sink(rules, opened, monkeypatch)

    sink.handle_ancs(_ancs("com.apple.mobilemail"))
    [call] = sink._notif.calls
    assert list(call[5]) == ["default", "Open"]
    assert bool(call[6]["transient"]) is True
    argv = json.loads(call[6]["omarchy-exec-argv"])
    assert argv[:3] == [sys.executable, "-m", "blueferry.notification_open"]
    assert len(argv) == 4 and argv[3].startswith("--click=")
    nid = sink._notif.next_id

    sink._on_activation_token(nid, "wayland-token")
    sink._on_action(nid, "default")

    assert opened == [
        (OpenTarget("desktop", "org.mozilla.Thunderbird.desktop"), "wayland-token"),
    ]
    # Nothing from the notification reaches the launcher.
    launched = repr(opened)
    for fragment in ("touch", "pwned", "rm -rf", "evil", "passwd", "javascript", "{body}"):
        assert fragment not in launched


def test_unmapped_app_popup_keeps_todays_behaviour(monkeypatch) -> None:
    opened = []
    sink = _clickable_sink({"com.apple.mobilemail": "https://mail.example.com/"}, opened, monkeypatch)

    sink.handle_ancs(_ancs("com.example.Other"))
    [call] = sink._notif.calls
    assert list(call[5]) == []
    nid = sink._notif.next_id

    sink._on_activation_token(nid, "token")
    sink._on_action(nid, "default")

    assert opened == []
    assert sink._open_apps == {}
    assert sink._activation_tokens == {}


def test_empty_mapping_matches_the_previous_popup_exactly(monkeypatch) -> None:
    sink = _clickable_sink({}, [], monkeypatch)

    sink.handle_ancs(_ancs("com.apple.mobilemail", title="Inbox", body="New mail"))

    # The exact Notify() arguments this popup had before click rules existed.
    [(app, replaces, icon, title, body, actions, hints, timeout)] = sink._notif.calls
    assert (app, int(replaces), icon) == ("BlueFerry", 0, "phone-symbolic")
    assert title == "\U0001f4f1 App \u00b7 Inbox"
    assert body == "New mail"
    assert list(actions) == []
    assert actions.signature == "s"
    assert dict(hints) == {"urgency": 1, "transient": True}
    assert set(hints) == {"urgency", "transient"}
    assert int(timeout) == _ANCS_EXPIRE_MS
    assert sink._open_apps == {}


def test_a_popup_opens_its_target_at_most_once(monkeypatch) -> None:
    opened = []
    now = [100.0]
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: now[0])
    sink = _clickable_sink({"com.slack": "slack.desktop"}, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    nid = sink._notif.next_id

    sink._on_action(nid, "default")
    now[0] += 5.0
    sink._on_action(nid, "default")

    assert len(opened) == 1


def test_rule_removed_after_the_popup_appeared_is_not_launched(monkeypatch) -> None:
    opened = []
    rules = {"net.whatsapp.WhatsApp": "https://web.whatsapp.com"}
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("net.whatsapp.WhatsApp"))
    rules.clear()

    sink._on_action(sink._notif.next_id, "default")

    assert opened == []


def test_rule_changed_after_the_popup_appeared_uses_the_current_target(monkeypatch) -> None:
    opened = []
    rules = {"net.whatsapp.WhatsApp": "https://web.whatsapp.com"}
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("net.whatsapp.WhatsApp"))
    rules["net.whatsapp.WhatsApp"] = "whatsapp.desktop"

    sink._on_action(sink._notif.next_id, "default")

    assert opened == [(OpenTarget("desktop", "whatsapp.desktop"), "")]


def test_click_rules_never_apply_to_other_actions_or_closed_popups(monkeypatch) -> None:
    opened = []
    sink = _clickable_sink({"com.slack": "slack.desktop"}, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    nid = sink._notif.next_id

    sink._on_action(nid, "dismiss")
    sink._on_action(nid + 100, "default")
    sink._on_closed(nid, 1)
    sink._on_action(nid, "default")

    assert opened == []
    assert sink._open_apps == {}


def test_repeated_clicks_on_one_target_launch_at_most_once_per_interval(monkeypatch) -> None:
    opened = []
    now = [100.0]
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: now[0])
    sink = _clickable_sink({"com.slack": "slack.desktop"}, opened, monkeypatch)
    for _ in range(3):
        sink.handle_ancs(_ancs("com.slack"))
    first = sink._notif.next_id - 2

    sink._on_action(first, "default")
    sink._on_action(first + 1, "default")
    now[0] += 1.5
    sink._on_action(first + 2, "default")

    assert len(opened) == 2


def test_a_throttled_click_leaves_the_popup_clickable(monkeypatch) -> None:
    opened = []
    now = [100.0]
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: now[0])
    sink = _clickable_sink({"com.slack": "slack.desktop"}, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    sink.handle_ancs(_ancs("com.slack"))
    second = sink._notif.next_id

    sink._on_action(second - 1, "default")
    sink._on_action(second, "default")
    assert second in sink._open_apps
    now[0] += 1.5
    sink._on_action(second, "default")

    assert len(opened) == 2
    assert sink._open_apps == {}


def test_clicks_on_different_targets_are_not_throttled_together(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: 100.0)
    rules = {"com.slack": "slack.desktop", "net.whatsapp.WhatsApp": "https://web.whatsapp.com"}
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    sink.handle_ancs(_ancs("net.whatsapp.WhatsApp"))
    last = sink._notif.next_id

    sink._on_action(last - 1, "default")
    sink._on_action(last, "default")

    assert [target.value for target, _token in opened] == [
        "slack.desktop", "https://web.whatsapp.com",
    ]


def test_a_failing_rule_lookup_leaves_the_popup_unclickable(monkeypatch) -> None:
    sink = _clickable_sink({}, [], monkeypatch)

    def broken(_app_id):
        raise RuntimeError("settings unavailable")

    sink._open_target = broken
    sink.handle_ancs(_ancs("com.slack"))

    assert list(sink._notif.calls[0][5]) == []


def test_messages_popups_and_legacy_sinks_are_unaffected(monkeypatch) -> None:
    monkeypatch.setattr("blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True)
    # A sink built without the new collaborators behaves as before.
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    sink.handle_ancs(_ancs("com.slack"))
    assert list(sink._notif.calls[0][5]) == []
    sink._on_action(1, "default")

    opened = []
    mapped = _clickable_sink({"com.apple.MobileSMS": "https://example.com"}, opened, monkeypatch)
    mapped.handle_ancs(_ancs("com.apple.MobileSMS"))
    assert mapped._notif.calls == []


def test_click_trackers_are_bounded_and_released_on_close(monkeypatch) -> None:
    sink = _clickable_sink({"com.slack": "slack.desktop"}, [], monkeypatch)
    monkeypatch.setattr(libnotify_mod, "MAX_NOTIFICATION_CLICK_TRACKERS", 2)
    for _ in range(4):
        sink.handle_ancs(_ancs("com.slack"))

    assert len(sink._open_apps) == 2
    assert ("close", 41) in sink._notif.calls

    sink._match = sink._action_match = sink._token_match = None
    sink.close()
    assert sink._open_apps == {}


def test_app_popups_never_evict_a_message_popup(monkeypatch) -> None:
    sink = _clickable_sink({"com.slack": "slack.desktop"}, [], monkeypatch)
    monkeypatch.setattr(libnotify_mod, "MAX_DESKTOP_MESSAGE_TRACKERS", 2)
    monkeypatch.setattr(libnotify_mod, "MAX_NOTIFICATION_CLICK_TRACKERS", 2)
    sink._open_messages = {7: "message-7"}
    sink._pending = {7: "/org/bluez/obex/message7"}

    for _ in range(5):
        sink.handle_ancs(_ancs("com.slack"))

    assert sink._open_messages == {7: "message-7"}
    assert sink._pending == {7: "/org/bluez/obex/message7"}
    assert ("close", 7) not in sink._notif.calls
    assert len(sink._open_apps) == 2


def test_message_popups_never_evict_a_clickable_app_popup(monkeypatch) -> None:
    sink = _clickable_sink({"com.slack": "slack.desktop"}, [], monkeypatch)
    monkeypatch.setattr(libnotify_mod, "MAX_DESKTOP_MESSAGE_TRACKERS", 1)
    sink.handle_ancs(_ancs("com.slack"))
    app_popup = sink._notif.next_id
    sink._open_messages = {1: "message-1", 2: "message-2"}

    sink._prune_trackers()

    assert sink._open_messages == {2: "message-2"}
    assert app_popup in sink._open_apps


def _shell_click_id(call) -> str:
    argv = json.loads(call[6]["omarchy-exec-argv"])
    return argv[-1].removeprefix("--click=")


def test_shell_argv_carries_neither_the_target_nor_the_app(monkeypatch) -> None:
    rules = {"com.example.Calendar": "https://cal.example.com/feed?token=s3cret"}
    sink = _clickable_sink(rules, [], monkeypatch)
    sink.handle_ancs(_ancs("com.example.Calendar"))
    sink.handle_ancs(_ancs("com.example.Calendar"))

    first, second = sink._notif.calls
    hints = repr(dict(first[6]))
    for fragment in ("s3cret", "cal.example.com", "com.example.Calendar", "--url", "--desktop-id"):
        assert fragment not in hints
    # Every popup gets its own unguessable ID.
    assert _shell_click_id(first) != _shell_click_id(second)
    assert len(_shell_click_id(first)) >= 22


def test_shell_clicks_take_the_same_path_as_live_clicks(monkeypatch) -> None:
    opened = []
    now = [100.0]
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: now[0])
    rules = {"com.slack": "slack.desktop"}
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    sink.handle_ancs(_ancs("com.slack"))
    first, second = (_shell_click_id(call) for call in sink._notif.calls)

    assert sink.open_click(first, "shell-token") is True
    # One-shot: the same popup's argv run again opens nothing.
    now[0] += 5.0
    assert sink.open_click(first, "shell-token") is False
    # Throttle: a second popup for the same target within a second waits.
    sink.handle_ancs(_ancs("com.slack"))
    third = _shell_click_id(sink._notif.calls[-1])
    assert sink.open_click(second, "") is True
    assert sink.open_click(third, "") is False
    # The throttled popup stays clickable.
    now[0] += 1.5
    assert sink.open_click(third, "") is True

    assert [token for _target, token in opened] == ["shell-token", "", ""]
    assert sink._click_ids == {}


def test_shell_click_after_rule_removal_or_expiry_opens_nothing(monkeypatch) -> None:
    opened = []
    rules = {"com.slack": "slack.desktop"}
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    sink.handle_ancs(_ancs("com.slack"))
    first, second = (_shell_click_id(call) for call in sink._notif.calls)

    # Reason 1: the popup expired; nobody clicked or dismissed it.
    sink._on_closed(sink._notif.next_id, 1)
    assert sink.open_click(second, "") is False
    rules.clear()
    assert sink.open_click(first, "") is False
    assert sink.open_click("unknown", "") is False
    assert sink.open_click("x" * 65, "") is False

    assert opened == []
    assert list(sink._click_ids.values()) == [sink._notif.next_id - 1]
    assert sink._dismissed_clicks == {}


def _dismissed_popup(rules, opened, monkeypatch, now, reason=2):
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: now[0])
    sink = _clickable_sink(rules, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    click_id = _shell_click_id(sink._notif.calls[0])
    sink._on_closed(sink._notif.next_id, reason)
    assert sink._click_ids == {} and sink._open_apps == {}
    return sink, click_id


def test_shell_click_right_after_the_shell_dismissed_the_popup_opens(monkeypatch) -> None:
    # Omarchy runs the argv and dismisses the popup at once, so the close
    # arrives before the helper's call.
    opened = []
    now = [100.0]
    sink, click_id = _dismissed_popup({"com.slack": "slack.desktop"}, opened, monkeypatch, now)

    now[0] += libnotify_mod._DISMISSED_CLICK_GRACE_S - 0.5
    assert sink.open_click(click_id, "shell-token") is True
    # One-shot: the same ID opens nothing a second time.
    now[0] += 2.0
    assert sink.open_click(click_id, "shell-token") is False

    assert opened == [(OpenTarget("desktop", "slack.desktop"), "shell-token")]
    assert sink._dismissed_clicks == {}


def test_shell_click_after_the_dismissal_grace_opens_nothing(monkeypatch) -> None:
    opened = []
    now = [100.0]
    sink, click_id = _dismissed_popup({"com.slack": "slack.desktop"}, opened, monkeypatch, now)

    now[0] += libnotify_mod._DISMISSED_CLICK_GRACE_S
    assert sink.open_click(click_id, "") is False

    assert opened == []
    assert sink._dismissed_clicks == {}


@pytest.mark.parametrize("reason", [1, 3, 4])
def test_only_a_dismissal_keeps_the_click_id(monkeypatch, reason) -> None:
    opened = []
    sink, click_id = _dismissed_popup(
        {"com.slack": "slack.desktop"}, opened, monkeypatch, [100.0], reason,
    )

    assert sink.open_click(click_id, "") is False
    assert opened == []
    assert sink._dismissed_clicks == {}


def test_rule_removed_during_the_dismissal_grace_opens_nothing(monkeypatch) -> None:
    opened = []
    rules = {"com.slack": "slack.desktop"}
    sink, click_id = _dismissed_popup(rules, opened, monkeypatch, [100.0])
    rules.clear()

    assert sink.open_click(click_id, "") is False
    assert opened == []


def test_a_dismissed_click_still_goes_through_the_throttle(monkeypatch) -> None:
    opened = []
    now = [100.0]
    sink, click_id = _dismissed_popup({"com.slack": "slack.desktop"}, opened, monkeypatch, now)
    sink.handle_ancs(_ancs("com.slack"))

    sink._on_action(sink._notif.next_id, "default")
    assert sink.open_click(click_id, "") is False
    now[0] += 1.5
    assert sink.open_click(click_id, "") is True

    assert len(opened) == 2


def test_a_popup_clicked_before_its_dismissal_keeps_no_click_id(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: 100.0)
    sink = _clickable_sink({"com.slack": "slack.desktop"}, opened, monkeypatch)
    sink.handle_ancs(_ancs("com.slack"))
    click_id = _shell_click_id(sink._notif.calls[0])

    sink._on_action(sink._notif.next_id, "default")
    sink._on_closed(sink._notif.next_id, 2)

    assert sink.open_click(click_id, "") is False
    assert len(opened) == 1


def test_dismissed_click_ids_are_bounded_and_released_on_close(monkeypatch) -> None:
    monkeypatch.setattr(libnotify_mod.time, "monotonic", lambda: 100.0)
    sink = _clickable_sink({"com.slack": "slack.desktop"}, [], monkeypatch)
    monkeypatch.setattr(libnotify_mod, "MAX_NOTIFICATION_CLICK_TRACKERS", 2)
    for _ in range(4):
        sink.handle_ancs(_ancs("com.slack"))
        sink._on_closed(sink._notif.next_id, 2)

    newest = [_shell_click_id(call) for call in sink._notif.calls[-2:]]
    assert list(sink._dismissed_clicks) == newest

    sink._match = sink._action_match = sink._token_match = None
    sink.close()
    assert sink._dismissed_clicks == {}


def test_evicted_and_closed_sinks_forget_click_ids(monkeypatch) -> None:
    sink = _clickable_sink({"com.slack": "slack.desktop"}, [], monkeypatch)
    monkeypatch.setattr(libnotify_mod, "MAX_NOTIFICATION_CLICK_TRACKERS", 1)
    sink.handle_ancs(_ancs("com.slack"))
    sink.handle_ancs(_ancs("com.slack"))

    assert list(sink._click_ids.values()) == [sink._notif.next_id]
    sink._match = sink._action_match = sink._token_match = None
    sink.close()
    assert sink._click_ids == {}


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


def test_ancs_popup_mirrors_the_iphone_title_subtitle_and_message(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        app_name="GitHub",
        app_id="com.github.stormbreaker.prod",
        title="Run succeeded",
        subtitle="octo-org/octo-repo",
        body="CI - v0.1.0 (bd753fb)",
    )

    sink.handle_ancs(event)

    [(_app, _replaces, _icon, title, body, *_rest)] = sink._notif.calls
    assert title == "\U0001f4f1 GitHub \u00b7 Run succeeded"
    assert body == "octo-org/octo-repo\nCI - v0.1.0 (bd753fb)"


def test_ancs_popup_hides_title_and_subtitle_without_content(monkeypatch) -> None:
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", False
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(
        app_name="GitHub",
        app_id="com.github.stormbreaker.prod",
        title="Run succeeded",
        subtitle="octo-org/octo-repo",
        body="CI - v0.1.0 (bd753fb)",
    )

    sink.handle_ancs(event)

    [(_app, _replaces, _icon, title, body, *_rest)] = sink._notif.calls
    assert title == "\U0001f4f1 GitHub"
    assert body == "New iPhone notification"


def _ancs_popup(monkeypatch, **fields):
    monkeypatch.setattr(
        "blueferry.sinks.libnotify.config.SHOW_NOTIFICATION_CONTENT", True
    )
    sink = LibnotifySink.__new__(LibnotifySink)
    sink._notification_policy = lambda: "all"
    sink._notif = _FakeNotifications()
    event = SimpleNamespace(**{
        "app_name": "GitHub",
        "app_id": "com.github.stormbreaker.prod",
        "title": "",
        "subtitle": "",
        "body": "",
        **fields,
    })
    sink.handle_ancs(event)
    [(_app, _replaces, _icon, title, body, *_rest)] = sink._notif.calls
    return title, body


@pytest.mark.parametrize("headline", ["github", " GitHub ", "GITHUB", "", "   "])
def test_ancs_popup_skips_a_title_that_only_repeats_the_app_or_is_blank(
    monkeypatch, headline,
) -> None:
    title, _body = _ancs_popup(monkeypatch, title=headline, body="hello")
    assert title == "\U0001f4f1 GitHub"


def test_ancs_popup_trims_title_and_drops_blank_lines(monkeypatch) -> None:
    title, body = _ancs_popup(
        monkeypatch, title="  Run succeeded ", subtitle="  ", body=" CI \n",
    )
    assert title == "\U0001f4f1 GitHub · Run succeeded"
    assert body == "CI"


def test_ancs_popup_shows_the_full_requested_subtitle_and_message(
    monkeypatch,
) -> None:
    subtitle = "s" * ANCS_SUBTITLE_MAX_BYTES
    message = "m" * ANCS_MESSAGE_MAX_BYTES
    _title, body = _ancs_popup(
        monkeypatch, title="t", subtitle=subtitle, body=message,
    )
    assert body == f"{subtitle}\n{message}"
