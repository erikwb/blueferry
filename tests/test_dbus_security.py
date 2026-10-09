"""Session-bus caller checks without opening any real D-Bus connection."""
from __future__ import annotations

import pytest

from blueferry.dbus_security import CallerGuard
from blueferry.errors import AuthorizationError, RateLimitError


def test_caller_guard_rejects_a_different_unix_user() -> None:
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1001},
    )

    with pytest.raises(AuthorizationError, match="not authorized"):
        guard.authorize(":1.20", "read")


@pytest.mark.parametrize("sender", [None, "", "io.weirdware.BlueFerry.Gtk"])
def test_caller_guard_requires_a_unique_bus_name(sender) -> None:
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
    )

    with pytest.raises(AuthorizationError, match="identity"):
        guard.authorize(sender, "status")


def test_consequential_calls_are_rate_limited_and_recover() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )

    for _ in range(6):
        guard.authorize(":1.20", "destructive")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "destructive")

    now[0] += 601
    guard.authorize(":1.20", "destructive")


def test_conversation_deletes_have_an_independent_interactive_quota() -> None:
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
    )

    for _ in range(7):
        guard.authorize(":1.20", "conversation-delete")
    for _ in range(6):
        guard.authorize(":1.20", "destructive")

    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "destructive")


def test_disconnecting_forgets_per_caller_state_but_not_global_quota() -> None:
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
    )
    guard.authorize(":1.20", "settings")

    assert ":1.20" in guard._credentials
    assert any(key[0] == ":1.20" for key in guard._attempts)

    guard.forget(":1.20")

    assert ":1.20" not in guard._credentials
    assert not any(key[0] == ":1.20" for key in guard._attempts)
    assert any(key[0] == "*" for key in guard._attempts)


def test_media_commands_have_their_own_quota_that_reconnects_cannot_reset() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )

    for _ in range(60):
        guard.authorize(":1.20", "media-command")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "media-command")
    # A new connection shares the daemon-wide bucket.
    with pytest.raises(RateLimitError):
        guard.authorize(":1.21", "media-command")
    # Media polling and command floods never starve messaging.
    guard.authorize(":1.20", "media-read")
    guard.authorize(":1.20", "read")
    guard.authorize(":1.20", "send")

    now[0] += 61
    guard.authorize(":1.21", "media-command")


def test_media_reads_do_not_consume_message_read_quota() -> None:
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
    )
    for _ in range(600):
        guard.authorize(":1.20", "media-read")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "media-read")
    guard.authorize(":1.20", "read")


def test_dialing_has_its_own_strict_quota() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )

    for _ in range(6):
        guard.authorize(":1.20", "calls-dial")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "calls-dial")
    # Answering or hanging up an existing call is never blocked by dialing,
    # nor does call control consume the message-send quota.
    for _ in range(30):
        guard.authorize(":1.20", "calls-control")
    guard.authorize(":1.20", "send")

    # A second connection cannot bypass the daemon-wide dial quota.
    with pytest.raises(RateLimitError):
        guard.authorize(":1.21", "calls-dial")
    now[0] += 61
    guard.authorize(":1.21", "calls-dial")


def test_dialing_is_bounded_per_hour() -> None:
    now = [0.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )
    for _ in range(60):
        guard.authorize(":1.20", "calls-dial")
        now[0] += 11
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "calls-dial")


def test_answering_has_its_own_bucket_and_never_blocks_hanging_up() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )

    for _ in range(10):
        guard.authorize(":1.20", "calls-answer")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "calls-answer")
    for _ in range(30):
        guard.authorize(":1.20", "calls-control")
    now[0] += 61
    guard.authorize(":1.20", "calls-answer")


def test_tether_commands_have_their_own_bucket() -> None:
    now = [100.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )

    for _ in range(10):
        guard.authorize(":1.20", "tether")
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "tether")
    # Toggling tethering never starves status reads or other settings.
    guard.authorize(":1.20", "status")
    guard.authorize(":1.20", "settings")
    # A reconnecting client does not get a fresh daemon-wide quota.
    with pytest.raises(RateLimitError):
        guard.authorize(":1.21", "tether")

    now[0] += 61
    guard.authorize(":1.20", "tether")



def test_tether_bucket_has_an_hourly_ceiling() -> None:
    now = [0.0]
    guard = CallerGuard(
        expected_uid=1000,
        credential_provider=lambda _sender: {"UnixUserID": 1000},
        clock=lambda: now[0],
    )
    for _ in range(6):
        for _ in range(10):
            guard.authorize(":1.20", "tether")
        now[0] += 61
    with pytest.raises(RateLimitError):
        guard.authorize(":1.20", "tether")
