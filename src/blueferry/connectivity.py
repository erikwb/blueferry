"""Connectivity state and retry policy independent of GLib and BlueZ."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectivityState(str, Enum):
    INITIALIZING = "initializing"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    AUTHORIZATION_REQUIRED = "authorization-required"
    MAP_CONNECTION_REFUSED = "map-connection-refused"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    initial_seconds: int = 5
    maximum_seconds: int = 300
    multiplier: int = 2

    def delay(self, failure_count: int) -> int:
        exponent = max(0, int(failure_count) - 1)
        return min(
            self.maximum_seconds,
            self.initial_seconds * (self.multiplier ** exponent),
        )


class Connectivity:
    """State machine for the daemon's MAP/PBAP connectivity lifecycle."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self.state = ConnectivityState.INITIALIZING
        self.detail = "backend startup"
        self.failure_count = 0
        self.retry_delay_seconds: int | None = None

    def connecting(self, detail: str = "opening MAP/PBAP sessions") -> None:
        if (
            self.failure_count > 0
            and self.state is ConnectivityState.MAP_CONNECTION_REFUSED
        ):
            # Keep the actionable refusal visible while the next poll is in
            # flight. Success or a different failure will replace it.
            self.retry_delay_seconds = None
            return
        self.state = (
            ConnectivityState.CONNECTING
            if self.failure_count == 0
            else ConnectivityState.RECONNECTING
        )
        self.detail = detail
        self.retry_delay_seconds = None

    def ready(self) -> None:
        self.state = ConnectivityState.READY
        self.detail = "MAP/PBAP sessions are available"
        self.failure_count = 0
        self.retry_delay_seconds = None

    def failed(
        self,
        error: Exception | str,
        *,
        authorization_required: bool = False,
        map_connection_refused: bool = False,
        retry_delay_seconds: int | None = None,
    ) -> int:
        self.failure_count += 1
        self.retry_delay_seconds = (
            max(1, int(retry_delay_seconds))
            if retry_delay_seconds is not None
            else self.policy.delay(self.failure_count)
        )
        if map_connection_refused:
            self.state = ConnectivityState.MAP_CONNECTION_REFUSED
        elif authorization_required:
            self.state = ConnectivityState.AUTHORIZATION_REQUIRED
        else:
            self.state = ConnectivityState.DEGRADED
        self.detail = str(error) or "Bluetooth profiles unavailable"
        return self.retry_delay_seconds

    def lost(self, reason: str) -> None:
        self.state = ConnectivityState.RECONNECTING
        self.detail = reason
        self.retry_delay_seconds = None

    def stopping(self) -> None:
        self.state = ConnectivityState.STOPPING
        self.detail = "backend stopping"
        self.retry_delay_seconds = None

    def snapshot(self) -> dict[str, object]:
        return {
            "connectivity_state": self.state.value,
            "connectivity_detail": self.detail,
            "retry_attempt": self.failure_count,
            "retry_delay_seconds": self.retry_delay_seconds or 0,
        }
