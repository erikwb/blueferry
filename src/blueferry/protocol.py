"""Stable identifiers for the private session D-Bus API."""

from collections.abc import Mapping

BUS_NAME = "io.weirdware.BlueFerry"
OBJECT_PATH = "/io/weirdware/BlueFerry"
MESSAGES_IFACE = f"{BUS_NAME}.Messages1"
EVENTS_IFACE = f"{BUS_NAME}.Events1"
# Desktop-presence features that are not part of messaging (the opt-in away
# lock). Its state is reported through Messages1.GetStatus.
PRESENCE_IFACE = f"{BUS_NAME}.Presence1"
MEDIA_IFACE = f"{BUS_NAME}.Media1"
# Optional, default-off HFP call control. The interface is always exported so
# introspection is stable; its methods fail with CallsDisabled when the
# feature is off. Calls1 does not change MESSAGES_API_VERSION.
CALLS_IFACE = f"{BUS_NAME}.Calls1"
ERROR_PREFIX = f"{BUS_NAME}.Error"

# Compatibility generation, independent of package versions. Generation 2
# requires roster-bound replies; additive compatible changes keep this value.
MESSAGES_API_VERSION = 2


def backend_compatibility_error(status: Mapping[str, object]) -> str | None:
    version = status.get("api_version")
    if type(version) is int and version == MESSAGES_API_VERSION:
        return None
    return (
        "This BlueFerry client is incompatible with the running backend. "
        "Install the most recent version of BlueFerry."
    )

# Read/control timeouts are part of the client policy rather than toolkit
# behavior. Keep them here so synchronous, GTK, Qt, and TUI callers agree.
STATUS_CALL_TIMEOUT_SEC = 10
SNAPSHOT_CALL_TIMEOUT_SEC = 20
CONTACT_CALL_TIMEOUT_SEC = 8
GROUP_ROUTE_CALL_TIMEOUT_SEC = 20
POLICY_CALL_TIMEOUT_SEC = 10
STORAGE_CALL_TIMEOUT_SEC = 135  # wallet I/O has its own 120-second cancellation deadline
CLEAR_CALL_TIMEOUT_SEC = 20
DELETE_CALL_TIMEOUT_SEC = 20
# A media command waits behind at most a few queued GATT round trips, each
# bounded by the backend's 10-second BlueZ call timeout.
MEDIA_CALL_TIMEOUT_SEC = 25

# oFono bounds its own call-control requests at 30 seconds (Dial waits for the
# phone to accept the call); leave room for the backend's reply.
CALL_CONTROL_TIMEOUT_SEC = 40

# One phonebook pull or incoming-body fetch may already be ahead of an
# interactive request on the serialized OBEX worker. This is a client-side
# D-Bus wait limit, not an individual Bluetooth transfer timeout.
OBEX_CALL_TIMEOUT_SEC = 240
