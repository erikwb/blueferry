"""Central safety and resource limits for local and Bluetooth input."""
from __future__ import annotations

# MAP messages are normally small, but allow attachments-sized text without
# letting a local D-Bus caller or malformed peer allocate memory without bound.
MAX_OUTGOING_BODY_BYTES = 256 * 1024
MAX_BMESSAGE_BYTES = 1024 * 1024

# A full phonebook can legitimately be large. This is a byte limit, separate
# from PBAP's MaxCount contact limit.
MAX_PHONEBOOK_BYTES = 64 * 1024 * 1024
MAX_VCARD_CHARS = 1024 * 1024
MAX_CONTACT_NAME_CHARS = 512
MAX_CONTACT_ADDRESS_CHARS = 320

# ANCS is serialized over a single Control Point. Beyond this depth, old
# notification requests are no longer useful and only delay current events.
MAX_ANCS_REQUESTS = 512
MAX_ANCS_APP_CACHE = 256
MAX_ANCS_PENDING_PER_APP = 128
MAX_ANCS_FINGERPRINTS = 10_000

# D-Bus snapshots are presentation data, not an unlimited archive export. Keep
# pathological messages or very busy threads from producing enormous replies;
# ListEvents supports explicit bounded history access with the same body cap.
MAX_CONVERSATION_EVENTS = 2_000
MAX_THREAD_MESSAGES = 500
MAX_THREAD_BODY_CHARS = 32 * 1024
MAX_REMOTE_PROPERTY_CHARS = 512

# Public D-Bus snapshots are deliberately smaller than the private archive.
# A client can ask for a narrower tail, but cannot make the daemon construct an
# unbounded JSON document or return a single enormous bus message.
MAX_EVENT_QUERY_LIMIT = 2_000
MAX_THREAD_QUERY_LIMIT = 1_000
MAX_THREAD_DELETE_COUNT = 1_000
MAX_STARRED_THREADS = 200
MAX_CONFIRMED_GROUPS = 200
MAX_THREAD_KEY_CHARS = 1024
MAX_RECENT_QUERY_LIMIT = 200
MAX_EVENT_KINDS = 16
MAX_EVENT_KIND_CHARS = 64
MAX_CONTACT_QUERY_CHARS = 256
MAX_CONTACT_RESULTS = 100
# Enumeration is paged so a large phonebook cannot exceed MAX_DBUS_JSON_BYTES
# in one reply. A record carries every address for one person, and a worst-case
# card with many long addresses runs to tens of kilobytes, so keep the page
# small enough that a full page stays far inside the reply cap.
MAX_CONTACT_PAGE = 150
MAX_DBUS_JSON_BYTES = 8 * 1024 * 1024

# Long-lived daemon queues and subscriptions must remain bounded even if a
# peer or desktop service stops completing work normally.
MAX_OBEX_PENDING_OPERATIONS = 256
MAX_DESKTOP_MESSAGE_TRACKERS = 256
MAX_PHONEBOOK_CONTACTS = 65_535
MAX_CONTACT_ADDRESSES_PER_CARD = 64
