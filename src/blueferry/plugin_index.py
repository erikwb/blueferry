"""The plugin store: curated index files that list installable plugins.

An index is a small JSON document served over https::

    {"version": 1, "plugins": [
      {"id": "io.weirdware.blueferry.immich_photos", "name": "Immich photos",
       "description": "Recent photos from your Immich server.",
       "repo": "https://github.com/joshii-h/blueferry-plugin-immich",
       "ref": "v0.2.0", "capabilities": ["photos"], "icon": "folder-pictures",
       "emoji": "", "screenshot": "", "min_blueferry": "0.8", "api_version": 1}
    ]}

The index is untrusted: every entry is validated field by field, entries
that fail are dropped, text is plain text, and only https Git URLs survive.
An entry without ``ref`` is listed as "coming soon" and cannot be installed.
Installing an entry always goes through the confirmed install flow of
:mod:`blueferry.plugin_manager`.

Fetching blocks (urllib with a timeout and a size limit); a cache with a TTL
below ``$XDG_CACHE_HOME/blueferry/plugin-index/`` keeps the store usable
offline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blueferry.plugin_api.manifest import version_tuple
from blueferry.plugin_manager import InstallError, InstallRecord, check_url

DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/joshii-h/blueferry-plugins-index/main/plugins-index.json"
)
FETCH_TIMEOUT_SEC = 10.0
MAX_INDEX_BYTES = 256 * 1024
# Entries need four container levels; allow room for future index metadata.
MAX_INDEX_DEPTH = 32
MAX_ENTRIES = 100
MAX_INDEXES = 8
CACHE_TTL_SEC = 6 * 3600
_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_ICON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^\d+(\.\d+){0,3}$")
_INDEX_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?/\S{1,400}$")

Fetch = Callable[[str], bytes]


class IndexFetchError(Exception):
    """An index could not be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    id: str
    name: str
    description: str
    repo: str
    ref: str
    capabilities: tuple[str, ...]
    icon: str = ""
    emoji: str = ""
    screenshot: str = ""
    min_blueferry: str = ""
    api_version: int = 1
    index: str = ""

    @property
    def available(self) -> bool:
        """Installable: a ref is set (otherwise the entry is "coming soon")."""
        return bool(self.ref)


@dataclass(frozen=True, slots=True)
class StoreItem:
    entry: IndexEntry
    installed: bool
    installed_ref: str = ""
    update_available: bool = False
    compatible: bool = True


@dataclass(frozen=True, slots=True)
class Catalog:
    items: tuple[StoreItem, ...]
    # (index url, problem) for indexes that failed or came from a stale cache
    problems: tuple[tuple[str, str], ...] = ()


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(text.split())[:limit]


def check_index_url(url: str) -> str:
    url = url.strip()
    if not _INDEX_URL.fullmatch(url):
        raise InstallError("an index URL must start with https://")
    return url


def parse_entry(raw: object, index: str = "") -> IndexEntry | None:
    """One validated entry, or None when anything about it is off."""
    if not isinstance(raw, dict):
        return None
    plugin_id, repo = raw.get("id"), raw.get("repo")
    if not isinstance(plugin_id, str) or len(plugin_id) > 120 or not _ID.fullmatch(plugin_id):
        return None
    try:
        repo = check_url(repo) if isinstance(repo, str) else None
    except InstallError:
        repo = None
    name = _text(raw.get("name"), 80)
    if repo is None or not name:
        return None
    ref = raw.get("ref") or ""
    if not isinstance(ref, str) or (ref and (not _REF.fullmatch(ref) or ".." in ref)):
        return None
    capabilities = raw.get("capabilities") or []
    if not isinstance(capabilities, list):
        return None
    caps = tuple(dict.fromkeys(
        cap for cap in capabilities[:8] if isinstance(cap, str) and _CAPABILITY.fullmatch(cap)
    ))
    icon = str(raw.get("icon")) if isinstance(raw.get("icon"), str) else ""
    screenshot = raw.get("screenshot") if isinstance(raw.get("screenshot"), str) else ""
    try:
        screenshot = check_index_url(screenshot) if screenshot else ""
    except InstallError:
        screenshot = ""
    minimum = str(raw.get("min_blueferry")) if isinstance(raw.get("min_blueferry"), str) else ""
    api = raw.get("api_version", 1)
    if isinstance(api, bool) or not isinstance(api, int) or not 1 <= api <= 999:
        return None
    return IndexEntry(
        id=plugin_id, name=name, description=_text(raw.get("description"), 300),
        repo=repo, ref=ref, capabilities=caps,
        icon=icon if _ICON.fullmatch(icon) else "",
        emoji=_text(raw.get("emoji"), 8), screenshot=screenshot,
        min_blueferry=minimum if _VERSION.fullmatch(minimum) else "",
        api_version=api, index=index,
    )


def _check_json_depth(text: str) -> None:
    """Bound nesting before decoding, independently of Python's stack limit.

    JSON syntax validation stays with the decoder. Only count containers
    outside string literals, including correct handling of escaped quotes.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_INDEX_DEPTH:
                raise IndexFetchError("the index is too deeply nested")
        elif char in "]}":
            depth -= 1


def parse_index(data: bytes, index: str = "") -> list[IndexEntry]:
    if len(data) > MAX_INDEX_BYTES:
        raise IndexFetchError("the index is too large")
    try:
        text = data.decode("utf-8")
        _check_json_depth(text)
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise IndexFetchError("the index is not valid JSON") from None
    if not isinstance(document, dict) or document.get("version") != 1:
        raise IndexFetchError("unsupported index format")
    plugins = document.get("plugins")
    if not isinstance(plugins, list):
        raise IndexFetchError("the index has no plugin list")
    entries: dict[str, IndexEntry] = {}
    for raw in plugins[:MAX_ENTRIES]:
        entry = parse_entry(raw, index)
        if entry is not None:
            entries.setdefault(entry.id, entry)
    return list(entries.values())


class _InsecureRedirect(urllib.error.URLError):
    pass


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a few redirects, each to https only.

    Checking only the final URL would let https -> http -> https chains
    through, with the middle hop readable and changeable on the network.
    """

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).startswith("https://"):
            raise _InsecureRedirect("the index redirected away from https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(HttpsOnlyRedirects())


def _https_fetch(url: str, *, opener: Callable[[], Any] = _opener) -> bytes:
    check_index_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "BlueFerry plugin store"})
    try:
        with opener().open(request, timeout=FETCH_TIMEOUT_SEC) as response:  # nosec B310
            if not response.geturl().startswith("https://"):
                raise IndexFetchError("the index redirected away from https")
            data = response.read(MAX_INDEX_BYTES + 1)
    except _InsecureRedirect:
        raise IndexFetchError("the index redirected away from https") from None
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise IndexFetchError(f"could not fetch the index ({type(error).__name__})") from None
    if len(data) > MAX_INDEX_BYTES:
        raise IndexFetchError("the index is too large")
    return data


def cache_dir() -> Path:
    home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(home) / "blueferry" / "plugin-index"


class PluginIndex:
    def __init__(
        self,
        *,
        fetch: Fetch = _https_fetch,
        cache: Path | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = CACHE_TTL_SEC,
    ) -> None:
        self._fetch = fetch
        self._cache = cache or cache_dir()
        self._clock = clock
        self._ttl = ttl

    def _cache_path(self, url: str) -> Path:
        return self._cache / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def load(self, url: str, *, refresh: bool = False) -> tuple[list[IndexEntry], str]:
        """*Blocking.* Entries of one index and a problem note ("" when fresh)."""
        path = self._cache_path(url)
        cached: bytes | None = None
        age = None
        try:
            cached = path.read_bytes()[: MAX_INDEX_BYTES + 1]
            age = self._clock() - path.stat().st_mtime
        except OSError:
            pass
        if cached is not None and age is not None and 0 <= age < self._ttl and not refresh:
            try:
                return parse_index(cached, url), ""
            except IndexFetchError:
                cached = None
        try:
            data = self._fetch(url)
            entries = parse_index(data, url)
        except IndexFetchError as error:
            if cached is not None:
                try:
                    return parse_index(cached, url), f"{error}; showing the cached list"
                except IndexFetchError:
                    pass
            raise
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        return entries, ""

    def catalog(
        self,
        urls: Sequence[str],
        *,
        installed: dict[str, str],
        records: dict[str, InstallRecord],
        blueferry_version: str,
        supported_api: frozenset[int],
        refresh: bool = False,
    ) -> Catalog:
        """*Blocking.* Every index merged (first wins per id), with install state.

        ``installed`` maps discovered plugin ids to their versions.
        """
        entries: dict[str, IndexEntry] = {}
        problems: list[tuple[str, str]] = []
        for url in list(dict.fromkeys(urls))[:MAX_INDEXES]:
            try:
                loaded, note = self.load(url, refresh=refresh)
            except IndexFetchError as error:
                problems.append((url, str(error)))
                continue
            if note:
                problems.append((url, note))
            for entry in loaded:
                entries.setdefault(entry.id, entry)
        items = []
        for entry in entries.values():
            record = records.get(entry.id)
            compatible = entry.api_version in supported_api
            if entry.min_blueferry:
                try:
                    compatible = compatible and (
                        version_tuple(blueferry_version) >= version_tuple(entry.min_blueferry)
                    )
                except ValueError:
                    compatible = False
            items.append(StoreItem(
                entry=entry,
                installed=entry.id in installed,
                installed_ref=record.ref_label if record else installed.get(entry.id, ""),
                update_available=bool(
                    record and entry.ref and record.ref != entry.ref
                    and not record.commit.startswith(entry.ref)
                ),
                compatible=compatible,
            ))
        return Catalog(tuple(items), tuple(problems))


def index_urls(settings: dict) -> list[str]:
    value = settings.get("indexes")
    if not isinstance(value, list):
        return [DEFAULT_INDEX_URL]
    urls = []
    for url in value:
        try:
            urls.append(check_index_url(str(url)))
        except InstallError:
            continue
    return urls[:MAX_INDEXES]


def search(items: Sequence[StoreItem], term: str) -> list[StoreItem]:
    needle = term.strip().casefold()
    if not needle:
        return list(items)
    return [
        item for item in items
        if needle in " ".join((item.entry.id, item.entry.name, item.entry.description,
                               *item.entry.capabilities)).casefold()
    ]
