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
