"""Declining a call with a message: saved replies, the backend flow, the presenter.

Everything is hermetic: fake call control, a fake OBEX queue, temporary
settings files.
"""
from __future__ import annotations

import json

import pytest

from blueferry import quick_reply_view as view
from blueferry.backend_operations import BackendDependencies, BackendOperations
from blueferry.calls.quick_replies import (
    MAX_REPLIES,
    MAX_REPLY_CHARS,
    SETTINGS_KEY,
    QuickReplySettings,
    default_replies,
    valid_replies,
)
from blueferry.dbus_service import MessagesService
from blueferry.errors import (
    CallsDisabledError,
    ConfigurationError,
    DeclinedNotSentError,
    InvalidArgumentsError,
    NotFoundError,
    NotReadyError,
    OperationFailedError,
    SendOutcomeUnknownError,
)
from blueferry.models import CallInfo
from blueferry.protocol import ERROR_PREFIX

NUMBER = "+41791234567"


# ---- saved replies ----------------------------------------------------------


def test_defaults_apply_until_a_list_is_saved(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = QuickReplySettings(path)

    assert settings.get() == default_replies()
    assert settings.get()[0] == "Can't talk now. I'll call you back."
    assert len(default_replies()) <= MAX_REPLIES

    assert settings.set(["  On my way  ", "Later"]) == ["On my way", "Later"]
    assert json.loads(path.read_text())[SETTINGS_KEY] == ["On my way", "Later"]
    assert QuickReplySettings(path).get() == ["On my way", "Later"]


def test_an_empty_list_means_own_text_only(tmp_path) -> None:
    path = tmp_path / "settings.json"
    QuickReplySettings(path).set([])

    assert QuickReplySettings(path).get() == []


def test_a_corrupt_saved_list_reads_as_the_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({SETTINGS_KEY: ["ok", 5]}))

    assert QuickReplySettings(path).get() == default_replies()


@pytest.mark.parametrize("values", [
    "one string", [""], ["   "], ["a\nb"], ["x" * (MAX_REPLY_CHARS + 1)], ["a", "a"],
    [str(index) for index in range(MAX_REPLIES + 1)], [None], {"a": 1},
])
def test_invalid_lists_are_refused(values) -> None:
    with pytest.raises(InvalidArgumentsError):
        valid_replies(values)


def test_a_reply_of_full_length_is_kept() -> None:
    text = "x" * MAX_REPLY_CHARS
    assert valid_replies([text]) == [text]


# ---- the backend flow -------------------------------------------------------


class _Sessions:
    map = object()
    pbap = None
    map_path = "/org/bluez/obex/client/session1"

    def __init__(self) -> None:
        self.errors: list[Exception] = []

    def report_error(self, error) -> None:
        self.errors.append(error)


class FakeCalls:
    enabled = True

    def __init__(self, *, number=NUMBER, state="incoming", decline_error=None) -> None:
        self.number = number
        self.state = state
        self.decline_error = decline_error
        self.requests: list[tuple] = []

    def snapshot(self):
        return {"calls_enabled": self.enabled, "calls_state": "ready", "calls_available": True}

    def ringing_number(self, call_id):
        if call_id != "voicecall01":
            raise NotFoundError("call no longer exists")
        if self.state not in {"incoming", "waiting"}:
            raise InvalidArgumentsError("call is not ringing")
        if not self.number:
            raise InvalidArgumentsError("the caller's number is unknown")
        return self.number

    def send_tones(self, call_id, tones, success, _failure):
        # For the rate-limit test: tones share no bucket with declining.
        self.requests.append(("tones", call_id, tones))
        success(None)

    def hangup(self, call_id, success, failure):
        self.requests.append(("hangup", call_id))
        if self.decline_error is not None:
            failure(self.decline_error)
            return
        success(None)


class FakeReplies:
    def __init__(self, replies=None, error=None) -> None:
        self.replies = list(replies if replies is not None else ["Later"])
        self.error = error

    def get(self):
        return list(self.replies)

    def set(self, values):
        if self.error is not None:
            raise self.error
        self.replies = valid_replies(values)
        return list(self.replies)


class Obex:
    """Runs a queued send at once; ``error`` makes it fail."""

    def __init__(self, error=None) -> None:
        self.error = error
        self.sent: list = []

    def __call__(self, operation, *, on_success, on_error):
        if self.error is not None:
            on_error(self.error)
            return
        self.sent.append(operation)
        on_success("/transfer/1")


def _operations(calls=None, replies=None, obex=None, sessions=None, on_sent=None):
    return BackendOperations(sessions or _Sessions(), BackendDependencies(
        calls=calls if calls is not None else FakeCalls(),
        quick_replies=replies if replies is not None else FakeReplies(),
        submit_obex=obex if obex is not None else Obex(),
        on_sent=on_sent,
    ))


def _decline(operations, call_id="voicecall01", text="Later"):
    results: list = []
    errors: list = []
    operations.decline_with_message(call_id, text, results.append, errors.append)
    return results, errors


def test_status_carries_the_replies_only_while_calls_are_on() -> None:
    on = _operations().status()
    calls = FakeCalls()
    calls.enabled = False
    off = _operations(calls=calls).status()

    assert on["calls_quick_replies"] == ["Later"]
    assert "calls_quick_replies" not in off
    assert "calls_quick_replies" not in BackendOperations(
        _Sessions(), BackendDependencies(calls=FakeCalls()),
    ).status()


def test_decline_first_then_send_to_the_callers_number() -> None:
    calls, obex, sent = FakeCalls(), Obex(), []
    operations = _operations(
        calls=calls, obex=obex, on_sent=lambda *args: sent.append(args),
    )

    results, errors = _decline(operations, text="On my way")

    assert errors == [] and results == ["/transfer/1"]
    assert calls.requests == [("hangup", "voicecall01")]
    assert len(obex.sent) == 1
    assert sent == [(NUMBER, "On my way", "/transfer/1")]  # recorded like a Send


@pytest.mark.parametrize(("calls", "text", "error"), [
    (FakeCalls(number=""), "Later", InvalidArgumentsError),
    (FakeCalls(state="active"), "Later", InvalidArgumentsError),
    (FakeCalls(), "   ", InvalidArgumentsError),
    (FakeCalls(number="*31#"), "Later", InvalidArgumentsError),
])
def test_nothing_is_declined_when_a_reply_is_impossible(calls, text, error) -> None:
    with pytest.raises(error):
        _decline(_operations(calls=calls), text=text)

    assert calls.requests == []


def test_an_unknown_call_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        _decline(_operations(), call_id="voicecall99")


def test_a_failed_decline_sends_nothing() -> None:
    obex = Obex()
    failure = OperationFailedError("Call", RuntimeError("org.ofono.Error.Failed"))

    results, errors = _decline(_operations(calls=FakeCalls(decline_error=failure), obex=obex))

    assert results == [] and errors == [failure] and obex.sent == []


def test_a_failed_send_keeps_the_call_declined_and_says_so() -> None:
    calls = FakeCalls()

    _results, errors = _decline(_operations(calls=calls, obex=Obex(RuntimeError("timeout"))))

    assert calls.requests == [("hangup", "voicecall01")]
    assert isinstance(errors[0], DeclinedNotSentError)
    assert errors[0].dbus_suffix == "DeclinedNotSent"
    assert "declined" in str(errors[0]) and "timeout" in str(errors[0])
    assert not errors[0].outcome_unknown


def test_an_unconfirmed_send_says_to_check_messages() -> None:
    _results, errors = _decline(_operations(obex=Obex(SendOutcomeUnknownError())))

    assert errors[0].outcome_unknown and "Check Messages" in str(errors[0])


def test_without_map_or_obex_worker_the_call_keeps_ringing() -> None:
    sessions = _Sessions()
    sessions.map = None
    calls = FakeCalls()

    with pytest.raises(NotReadyError, match="message connection"):
        _decline(_operations(calls=calls, sessions=sessions))
    no_worker = BackendOperations(_Sessions(), BackendDependencies(
        calls=calls, quick_replies=FakeReplies(),
    ))
    with pytest.raises(NotReadyError, match="OBEX"):
        _decline(no_worker)

    assert calls.requests == []


def test_declining_with_a_message_needs_calls_and_the_capability() -> None:
    calls = FakeCalls()
    calls.enabled = False
    with pytest.raises(CallsDisabledError):
        _decline(_operations(calls=calls))
    with pytest.raises(NotReadyError):
        _decline(BackendOperations(_Sessions(), BackendDependencies(calls=FakeCalls())))


def test_saving_replies_is_validated_and_typed() -> None:
    replies = FakeReplies()
    operations = _operations(replies=replies)

    assert operations.set_call_quick_replies(["A", "B"]) == {"calls_quick_replies": ["A", "B"]}
    with pytest.raises(InvalidArgumentsError):
        operations.set_call_quick_replies(["A", "A"])
    with pytest.raises(NotReadyError):
        _operations(replies=FakeReplies(error=OSError("disk full"))).set_call_quick_replies([])


def test_unreadable_settings_refuse_the_replies_as_a_configuration_error(tmp_path) -> None:
    # UnreadableSettingsError is also a ValueError; it must not read as the
    # caller's mistake (InvalidArgs).
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    operations = _operations(replies=QuickReplySettings(path))

    with pytest.raises(ConfigurationError) as caught:
        operations.set_call_quick_replies(["Later"])

    error = MessagesService._dbus_error(caught.value)
    assert error.get_dbus_name() == f"{ERROR_PREFIX}.Configuration"
    assert error.get_dbus_message().startswith(
        "settings.json could not be read, so the change was not saved"
    )
    assert path.read_text(encoding="utf-8") == "{not json"


# ---- the presenter ----------------------------------------------------------


def _ringing(**extra):
    return CallInfo.from_dict({"call_id": "voicecall01", "state": "incoming",
                               "number": NUMBER, **extra})


def test_offered_only_for_ringing_calls_with_a_number() -> None:
    status = {"calls_quick_replies": ["Later"]}

    assert view.can_decline_with_message(status, _ringing())
    assert view.can_decline_with_message(status, {"state": "waiting", "number": NUMBER})
    assert not view.can_decline_with_message(status, _ringing(number=""))
    assert not view.can_decline_with_message(status, _ringing(state="active"))
    assert not view.can_decline_with_message({}, _ringing())  # old daemon or calls off
    assert not view.can_decline_with_message(status, None)


def test_choices_list_the_replies_then_own_text() -> None:
    choices = view.choices({"calls_quick_replies": ["Later", "Later", 5, "Busy"]})

    assert [choice["value"] for choice in choices] == ["Later", "Busy", view.CUSTOM]
    assert choices[-1]["label"] == "Own text…"
    assert view.choices({"calls_quick_replies": []}) == [
        {"value": view.CUSTOM, "label": "Own text…"},
    ]
    assert view.custom_text_error("  ") and not view.custom_text_error("ok")


def test_reply_editing_rules_match_the_daemon() -> None:
    existing = ["A", "B"]

    assert view.reply_error("C", existing) == ""
    assert view.reply_error("", existing)
    assert view.reply_error("A", existing)
    assert view.reply_error("A", existing, editing=0) == ""
    assert view.reply_error("a\nb", existing)
    assert view.reply_error("x" * (view.MAX_REPLY_CHARS + 1), existing)
    full = [str(index) for index in range(view.MAX_REPLIES)]
    assert view.reply_error("new", full) and not view.can_add(full)
    assert view.with_added(existing, " C ") == ["A", "B", "C"]
    assert view.with_replaced(existing, 1, "Z") == ["A", "Z"]
    assert view.with_removed(existing, 0) == ["B"]
    assert view.MAX_REPLIES == MAX_REPLIES and view.MAX_REPLY_CHARS == MAX_REPLY_CHARS


def test_settings_view_explains_an_empty_list() -> None:
    section = view.settings_view({"calls_quick_replies": []})

    assert section["supported"] and section["replies"] == [] and section["can_add"]
    assert "own text" in section["empty"]
    assert view.settings_view({})["supported"] is False


# ---- the daemon's popup path ------------------------------------------------


def test_popup_decline_with_message_uses_the_backend_and_reports_unsent(make_daemon) -> None:
    instance = make_daemon()
    calls: list[tuple] = []
    announced: list[int] = []

    class Operations:
        def decline_with_message(self, call_id, text, _success, failure):
            calls.append((call_id, text))
            failure(DeclinedNotSentError(RuntimeError("timeout")))

    instance._dbus_service = type("Service", (), {"operations": Operations()})()
    instance.events.call_reply_failed = lambda declined=True: announced.append(declined)

    instance._notification_call_action("voicecall01", "decline-message", "Later")

    assert calls == [("voicecall01", "Later")] and announced == [True]


def test_popup_reply_refused_before_declining_is_announced(make_daemon) -> None:
    instance = make_daemon()
    announced: list[bool] = []

    class Operations:
        def decline_with_message(self, _call_id, _text, _success, _failure):
            raise NotReadyError("MAP session not open")

    instance._dbus_service = type("Service", (), {"operations": Operations()})()
    instance.events.call_reply_failed = lambda declined=True: announced.append(declined)

    instance._notification_call_action("voicecall01", "decline-message", "Later")

    assert announced == [False]


def test_popup_replies_are_empty_while_calls_are_off(make_daemon) -> None:
    instance = make_daemon()

    assert instance._notification_quick_replies() == []
def test_lengths_are_capped_in_one_place() -> None:
    assert view.custom_text_error("x" * view.MAX_CUSTOM_CHARS) == ""
    assert "1000" in view.custom_text_error("x" * (view.MAX_CUSTOM_CHARS + 1))
    dialog = view.dialog_view({"calls_quick_replies": ["Later"]})
    assert dialog["max_chars"] == view.MAX_CUSTOM_CHARS and dialog["custom"] == view.CUSTOM
    section = view.settings_view({"calls_quick_replies": []})
    assert section["max_chars"] == MAX_REPLY_CHARS and section["max_replies"] == MAX_REPLIES
