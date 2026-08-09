from __future__ import annotations

from blueferry.ancs.sequencer import RequestBacklog


def test_backlog_is_bounded_and_coalesces_reserved_keys() -> None:
    backlog = RequestBacklog[str](2)

    assert backlog.enqueue("notification:1", "first") is True
    assert backlog.enqueue("notification:1", "modified") is False
    assert backlog.enqueue("notification:2", "second") is True
    assert backlog.enqueue("notification:3", "overflow") is False
    assert len(backlog) == 2


def test_key_remains_reserved_while_request_is_active() -> None:
    backlog = RequestBacklog[str](1)
    backlog.enqueue("notification:1", "first")

    assert backlog.popleft() == "first"
    assert backlog.enqueue("notification:1", "duplicate") is False

    backlog.finish("notification:1")
    assert backlog.enqueue("notification:1", "new") is True
