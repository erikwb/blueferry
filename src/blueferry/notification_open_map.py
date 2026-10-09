"""Validated per-app targets for clicking a mirrored iPhone notification.

A mapping connects one exact iPhone bundle ID to exactly one of:

* an ``http``/``https`` URL, opened with the desktop's default handler, or
* a desktop-entry ID such as ``org.mozilla.Thunderbird.desktop``, launched
  through Gio without files or URIs.

Targets are fixed user configuration. Nothing from a notification (title,
body, sender, app name) is ever interpolated into a target, and no target is
ever passed to a shell. Validation is deliberately stricter than the URL and
desktop-entry specifications: anything unusual is rejected rather than
normalised, so a rule means exactly what the user typed.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from blueferry.ancs.constants import MESSAGES_APP_ID
from blueferry.limits import (
    MAX_NOTIFICATION_OPEN_MAPPINGS,
    MAX_NOTIFICATION_OPEN_URL_CHARS,
)

OPEN_MAP_SETTINGS_KEY = "notification_open_map"
TargetKind = Literal["url", "desktop"]
URL_TARGET: Final = "url"
DESKTOP_TARGET: Final = "desktop"

_MAX_BUNDLE_ID_CHARS = 255
_MAX_DESKTOP_ID_CHARS = 255
# Reverse-DNS bundle IDs: at least two dot-separated labels, each starting
# with an ASCII letter or digit. ANCS reports the exact, case-sensitive ID.
_BUNDLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+")
# A desktop-file ID is a basename; '-' may encode a subdirectory, '/' never
# appears. Requiring the suffix keeps IDs visibly distinct from URLs.
_DESKTOP_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*\.desktop")
_URL_SCHEMES = frozenset({"http", "https"})
# RFC 3986 excludes these unescaped; quotes and backticks are rejected as well
# so no mapping ever looks like shell or markup even though none is used.
_URL_FORBIDDEN = frozenset('"\'`<>\\{}|^')
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
# Dot-separated labels (underscores occur in real hostnames) or an IPv6
# literal, which urlsplit returns without its brackets.
_HOST_LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
_HOST_RE = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*|[0-9a-f.]*:[0-9a-f:.]*")


@dataclass(frozen=True, slots=True)
class OpenTarget:
    kind: TargetKind
    value: str


def validate_bundle_id(value: object) -> str:
    """Return one exact, non-Messages iPhone bundle ID or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError("bundle ID must be a string")
    selected = value.strip()
    if not selected or len(selected) > _MAX_BUNDLE_ID_CHARS:
        raise ValueError("bundle ID must be 1 to 255 characters")
    if not _BUNDLE_ID_RE.fullmatch(selected):
        raise ValueError("bundle ID must look like com.example.App")
    if selected.casefold() == MESSAGES_APP_ID.casefold():
        raise ValueError("Messages notifications already open the conversation")
    return selected


def _validate_url(value: str) -> str:
    if len(value) > MAX_NOTIFICATION_OPEN_URL_CHARS:
        raise ValueError("URL is too long")
    if any(ord(character) <= 0x20 or ord(character) >= 0x7F for character in value):
        raise ValueError("URL must be printable ASCII without spaces")
    if any(character in _URL_FORBIDDEN for character in value):
        raise ValueError("URL contains a character that must be percent-encoded")
    if _BAD_PERCENT_RE.search(value):
        raise ValueError("URL contains an invalid percent escape")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise ValueError("URL is malformed") from error
    scheme = parts.scheme.lower()
    if scheme not in _URL_SCHEMES or not value[: len(scheme) + 3].lower() == f"{scheme}://":
        raise ValueError("URL must start with http:// or https://")
    if "@" in parts.netloc:
        raise ValueError("URL must not contain credentials")
    if parts.netloc.endswith(":"):
        raise ValueError("URL has an empty port")
    try:
        host = parts.hostname or ""
        _ = parts.port  # parsing validates the port range
    except ValueError as error:
        raise ValueError("URL has an invalid host or port") from error
    if not host or not _HOST_RE.fullmatch(host):
        raise ValueError("URL must name a host")
    return value


def _validate_desktop_id(value: str) -> str:
    if len(value) > _MAX_DESKTOP_ID_CHARS or not _DESKTOP_ID_RE.fullmatch(value):
        raise ValueError(
            "desktop entry must be an ID such as org.mozilla.Thunderbird.desktop"
        )
    return value


def parse_target(value: object) -> OpenTarget:
    """Classify and validate one configured click target."""
    if not isinstance(value, str):
        raise ValueError("target must be a string")
    selected = value.strip()
    if "://" in selected or ":" in selected.split("/", 1)[0]:
        # Anything with a scheme is a URL and must be http(s); this rejects
        # javascript:, file:, data: and custom handlers explicitly.
        return OpenTarget(URL_TARGET, _validate_url(selected))
    if selected.endswith(".desktop"):
        return OpenTarget(DESKTOP_TARGET, _validate_desktop_id(selected))
    raise ValueError(
        "target must be an http(s) URL or a desktop entry ID ending in .desktop"
    )


def normalize_open_map(raw: object) -> dict[str, str]:
    """Keep only valid rules from stored data; invalid entries fail closed."""
    if not isinstance(raw, Mapping):
        return {}
    mapping: dict[str, str] = {}
    for bundle_id, target in raw.items():
        if len(mapping) >= MAX_NOTIFICATION_OPEN_MAPPINGS:
            break
        try:
            mapping[validate_bundle_id(bundle_id)] = parse_target(target).value
        except ValueError:
            continue
    return mapping


def resolve_open_target(mapping: Mapping[str, str], bundle_id: object) -> OpenTarget | None:
    """Return the target for one ANCS app ID, or None for today's behaviour.

    Lookup is exact. The stored value is revalidated so a hand-edited
    settings file can never reach the launcher unchecked.
    """
    if not isinstance(bundle_id, str) or bundle_id.casefold() == MESSAGES_APP_ID.casefold():
        return None
    target = mapping.get(bundle_id)
    if target is None:
        return None
    try:
        return parse_target(target)
    except ValueError:
        return None


def open_map_entries(mapping: Mapping[str, str]) -> list[dict[str, str]]:
    """Stable, typed wire representation sorted by bundle ID."""
    entries = []
    for bundle_id in sorted(mapping):
        target = resolve_open_target(mapping, bundle_id)
        if target is not None:
            entries.append({"bundle_id": bundle_id, "target": target.value, "kind": target.kind})
    return entries
