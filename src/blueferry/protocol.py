"""Stable identifiers for the private session D-Bus API."""

BUS_NAME = "io.weirdware.BlueFerry"
OBJECT_PATH = "/io/weirdware/BlueFerry"
MESSAGES_IFACE = f"{BUS_NAME}.Messages1"
EVENTS_IFACE = f"{BUS_NAME}.Events1"
ERROR_PREFIX = f"{BUS_NAME}.Error"

# Read/control timeouts are part of the client policy rather than toolkit
# behavior. Keep them here so synchronous, GTK, Qt, and TUI callers agree.
STATUS_CALL_TIMEOUT_SEC = 10
SNAPSHOT_CALL_TIMEOUT_SEC = 20
CONTACT_CALL_TIMEOUT_SEC = 8
GROUP_ROUTE_CALL_TIMEOUT_SEC = 20
POLICY_CALL_TIMEOUT_SEC = 10
STORAGE_CALL_TIMEOUT_SEC = 30
CLEAR_CALL_TIMEOUT_SEC = 20

# One phonebook pull or incoming-body fetch may already be ahead of an
# interactive request on the serialized OBEX worker. This is a client-side
# D-Bus wait limit, not an individual Bluetooth transfer timeout.
OBEX_CALL_TIMEOUT_SEC = 240
