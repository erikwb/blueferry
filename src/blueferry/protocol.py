"""Stable identifiers for the private session D-Bus API."""

BUS_NAME = "io.weirdware.BlueFerry"
OBJECT_PATH = "/io/weirdware/BlueFerry"
MESSAGES_IFACE = f"{BUS_NAME}.Messages1"
EVENTS_IFACE = f"{BUS_NAME}.Events1"
ERROR_PREFIX = f"{BUS_NAME}.Error"

# One phonebook pull or incoming-body fetch may already be ahead of an
# interactive request on the serialized OBEX worker. This is a client-side
# D-Bus wait limit, not an individual Bluetooth transfer timeout.
OBEX_CALL_TIMEOUT_SEC = 240
