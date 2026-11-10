"""`blueferry calls decline-with-message` and `calls replies` over a fake client."""
from __future__ import annotations

from blueferry.models import BackendStatus
from tests.test_cli_calls import FakeClient, _call, _invoke


class ReplyClient(FakeClient):
    def __init__(self, *args, replies=("Later", "Busy"), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.replies = replies

    def status(self):
        value = {} if self.replies is None else {"calls_quick_replies": list(self.replies)}
        return BackendStatus.from_dict(value)

    def decline_call_with_message(self, call_id, text):
        self._record("decline_message", call_id, text)

    def set_call_quick_replies(self, replies):
        self._record("replies", list(replies))
        return {"calls_quick_replies": list(replies)}


def test_decline_with_the_first_reply_by_default(monkeypatch) -> None:
    client = ReplyClient([_call()])

    result = _invoke(monkeypatch, client, "decline-with-message")

    assert result.exit_code == 0 and "Declined and sent the message." in result.output
    assert client.requests == [("decline_message", "voicecall01", "Later")]


def test_decline_with_a_chosen_reply_or_own_text(monkeypatch) -> None:
    client = ReplyClient([_call()])

    second = _invoke(monkeypatch, client, "decline-with-message", "--reply", "2")
    own = _invoke(monkeypatch, client, "decline-with-message", "voicecall01", "--text", "Hi")
    both = _invoke(monkeypatch, client, "decline-with-message", "--text", "x", "--reply", "1")
    missing = _invoke(monkeypatch, client, "decline-with-message", "--reply", "9")

    assert second.exit_code == own.exit_code == 0
    assert both.exit_code == missing.exit_code == 2
    assert client.requests == [
        ("decline_message", "voicecall01", "Busy"), ("decline_message", "voicecall01", "Hi"),
    ]


def test_decline_without_replies_needs_own_text(monkeypatch) -> None:
    client = ReplyClient([_call()], replies=())

    result = _invoke(monkeypatch, client, "decline-with-message")

    assert result.exit_code == 2 and "--text" in result.output
    assert client.requests == []


def test_decline_on_an_old_daemon_says_so(monkeypatch) -> None:
    result = _invoke(monkeypatch, ReplyClient([_call()], replies=None), "decline-with-message")

    assert result.exit_code == 3 and "calls on" in result.output


def test_decline_failure_shows_the_daemon_text(monkeypatch) -> None:
    client = ReplyClient([_call()], error="The call was declined, but the message was not sent")

    result = _invoke(monkeypatch, client, "decline-with-message", "voicecall01", "--text", "x")

    assert result.exit_code == 3 and "declined, but" in result.output


def test_replies_list_set_and_clear(monkeypatch) -> None:
    client = ReplyClient(replies=("Later", "Evil\x1b[2J"))

    listed = _invoke(monkeypatch, client, "replies")
    saved = _invoke(monkeypatch, client, "replies", "set", "One", "Two")
    cleared = _invoke(monkeypatch, client, "replies", "clear")

    assert "1. Later" in listed.output and "\x1b[2J" not in listed.output
    assert "Saved 2 replies." in saved.output
    assert cleared.exit_code == 0
    assert client.requests == [("replies", ["One", "Two"]), ("replies", [])]
    client.replies = ()
    assert "No saved replies" in _invoke(monkeypatch, client, "replies").output
