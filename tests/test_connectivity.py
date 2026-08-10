"""Connectivity state and bounded backoff contracts."""
from blueferry.connectivity import Connectivity, ConnectivityState, RetryPolicy


def test_failures_back_off_to_a_bound_and_success_resets_them():
    connectivity = Connectivity(RetryPolicy(
        initial_seconds=5, maximum_seconds=20, multiplier=2
    ))

    assert [connectivity.failed("offline") for _ in range(4)] == [5, 10, 20, 20]
    assert connectivity.state is ConnectivityState.DEGRADED
    assert connectivity.snapshot()["retry_attempt"] == 4

    connectivity.connecting()
    assert connectivity.state is ConnectivityState.RECONNECTING
    connectivity.ready()

    assert connectivity.state is ConnectivityState.READY
    assert connectivity.failure_count == 0
    assert connectivity.retry_delay_seconds is None


def test_forbidden_profile_failure_is_user_actionable():
    connectivity = Connectivity()

    connectivity.failed("Forbidden", authorization_required=True)

    assert connectivity.snapshot() == {
        "connectivity_state": "authorization-required",
        "connectivity_detail": "Forbidden",
        "retry_attempt": 1,
        "retry_delay_seconds": 5,
    }


def test_map_refusal_preserves_detail_with_an_explicit_retry_interval():
    connectivity = Connectivity()

    delay = connectivity.failed(
        "Connection refused (111)",
        map_connection_refused=True,
        retry_delay_seconds=15,
    )

    assert delay == 15
    assert connectivity.snapshot() == {
        "connectivity_state": "map-connection-refused",
        "connectivity_detail": "Connection refused (111)",
        "retry_attempt": 1,
        "retry_delay_seconds": 15,
    }

    connectivity.connecting()

    assert connectivity.state is ConnectivityState.MAP_CONNECTION_REFUSED
    assert connectivity.detail == "Connection refused (111)"
    assert connectivity.retry_delay_seconds is None
