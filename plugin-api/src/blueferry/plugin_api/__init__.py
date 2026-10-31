"""Contract between BlueFerry clients and out-of-process plugins.

Self-contained on purpose: this package imports nothing else from
``blueferry``; it also ships as the distribution ``blueferry-plugin-api``. Plugins
import only from here. See PLUGINS.md for the interface contract.
"""
from __future__ import annotations

# Version of the Plugin1 contract (manifest keys, Plugin1 and the capability
# interfaces below). Additive changes keep the number; anything a v1 client
# could misread bumps it.
API_VERSION = 1
# Compatible additions within API_VERSION: 1 added the optional settings
# schema (``[Config <key>]``) with Plugin1.GetConfig/SetConfig; 2 added the
# generic UI surfaces (capabilities ``card``, ``share`` and ``notify``); 3
# added the guided settings form (placeholders, examples, help links, groups,
# pre-checks, ShowIf) with Plugin1.TestConfig and the ConfigLogin browser
# sign-in; 4 added card actions that send files (``send_to``), the manifest
# key ``ReplacesTools`` and the standard plugin log file; 5 made popups
# content-free: the ``notify`` capability emits ``NotifyChanged()`` and the
# host fetches the popup with ``GetPendingNotification()`` (the 1.2 signal
# ``Notify`` that carried the text on the bus is gone); 6 added buttons in
# the settings form (``[ConfigAction <id>]`` with Plugin1.RunConfigAction)
# and named help links (``HelpUrlLabel``). A manifest that needs a newer
# minor than this is ignored with a message.
API_MINOR = 6
SUPPORTED_API_VERSIONS = frozenset({1})

BUS_NAME_PREFIX = "io.weirdware.BlueFerry.Plugin."
# The BlueFerry daemon's well-known session-bus name. Only its owner may
# take waiting popups (``GetPendingNotification``, ApiVersion 1.5).
HOST_BUS_NAME = "io.weirdware.BlueFerry"
OBJECT_PATH = "/io/weirdware/BlueFerry/Plugin"
PLUGIN_INTERFACE = "io.weirdware.BlueFerry.Plugin1"
PHOTOS_INTERFACE = "io.weirdware.BlueFerry.Photos1"
# ApiVersion 1.2 surfaces: every method and signal lives on PLUGIN_INTERFACE
# at OBJECT_PATH; the manifest's capabilities decide which the host calls.
SURFACES_INTERFACE = PLUGIN_INTERFACE
METHOD_GET_CARD_ITEMS = "GetCardItems"
METHOD_INVOKE_ACTION = "InvokeAction"
METHOD_SHARE_TARGETS = "ShareTargets"
METHOD_SEND_FILES = "SendFiles"
SIGNAL_CARD_CHANGED = "CardChanged"
# ApiVersion 1.5: content-free popups.
SIGNAL_NOTIFY_CHANGED = "NotifyChanged"
METHOD_GET_PENDING_NOTIFICATION = "GetPendingNotification"
# ApiVersion 1.3 settings helpers, also on PLUGIN_INTERFACE. TestConfig is
# offered with ``ConfigTest=true``; the ConfigLogin* trio with
# ``ConfigLogin=<provider>`` in the [BlueFerry Plugin] group.
METHOD_TEST_CONFIG = "TestConfig"
METHOD_CONFIG_LOGIN = "ConfigLogin"
METHOD_CONFIG_LOGIN_STATUS = "ConfigLoginStatus"
METHOD_CONFIG_LOGIN_CANCEL = "ConfigLoginCancel"
# ApiVersion 1.6: a button of the settings form (``[ConfigAction <id>]``).
METHOD_RUN_CONFIG_ACTION = "RunConfigAction"
# Browser sign-in flows clients know how to label; others are ignored.
LOGIN_NEXTCLOUD = "nextcloud"
LOGIN_PROVIDERS = frozenset({LOGIN_NEXTCLOUD})

CAPABILITY_PHOTOS = "photos"
CAPABILITY_CARD = "card"
CAPABILITY_SHARE = "share"
CAPABILITY_NOTIFY = "notify"
KNOWN_CAPABILITIES = frozenset({
    CAPABILITY_PHOTOS, "conversations", CAPABILITY_CARD, CAPABILITY_SHARE, CAPABILITY_NOTIFY,
})

# ApiVersion 1.4: companion tools a plugin can stand in for
# (``ReplacesTools=localsend;``). The clients hide a replaced tool while the
# plugin is enabled; unknown names are ignored.
TOOL_LOCALSEND = "localsend"
TOOL_UXPLAY = "uxplay"
KNOWN_TOOLS = frozenset({TOOL_LOCALSEND, TOOL_UXPLAY})

# Everything a plugin returns is untrusted: clients reject larger replies.
MAX_REPLY_BYTES = 512 * 1024
MAX_RECENT_PHOTOS = 200

__all__ = [
    "API_MINOR",
    "API_VERSION",
    "BUS_NAME_PREFIX",
    "CAPABILITY_CARD",
    "CAPABILITY_NOTIFY",
    "CAPABILITY_PHOTOS",
    "CAPABILITY_SHARE",
    "HOST_BUS_NAME",
    "KNOWN_CAPABILITIES",
    "KNOWN_TOOLS",
    "LOGIN_NEXTCLOUD",
    "LOGIN_PROVIDERS",
    "MAX_RECENT_PHOTOS",
    "MAX_REPLY_BYTES",
    "METHOD_CONFIG_LOGIN",
    "METHOD_CONFIG_LOGIN_CANCEL",
    "METHOD_CONFIG_LOGIN_STATUS",
    "METHOD_GET_CARD_ITEMS",
    "METHOD_GET_PENDING_NOTIFICATION",
    "METHOD_INVOKE_ACTION",
    "METHOD_RUN_CONFIG_ACTION",
    "METHOD_SEND_FILES",
    "METHOD_SHARE_TARGETS",
    "METHOD_TEST_CONFIG",
    "OBJECT_PATH",
    "PHOTOS_INTERFACE",
    "PLUGIN_INTERFACE",
    "SIGNAL_CARD_CHANGED",
    "SIGNAL_NOTIFY_CHANGED",
    "SUPPORTED_API_VERSIONS",
    "SURFACES_INTERFACE",
    "TOOL_LOCALSEND",
    "TOOL_UXPLAY",
]
