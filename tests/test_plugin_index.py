"""The plugin store index: untrusted JSON, cache with TTL, install state."""
from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blueferry.plugin_index import (
    DEFAULT_INDEX_URL,
    MAX_INDEX_BYTES,
    IndexFetchError,
    PluginIndex,
    index_urls,
    parse_entry,
    parse_index,
    search,
)
from blueferry.plugin_manager import InstallError, InstallRecord

IMMICH = {
    "id": "io.weirdware.blueferry.immich_photos", "name": "Immich photos",
    "description": "Recent photos from your own Immich server.",
    "repo": "https://github.com/joshii-h/blueferry-plugin-immich", "ref": "v0.2.0",
    "capabilities": ["photos"], "icon": "folder-pictures", "emoji": "📷",
    "min_blueferry": "0.8", "api_version": 1,
}
SOON = {
    "id": "io.weirdware.blueferry.calendar", "name": "Calendar",
    "description": "Soon.", "repo": "https://github.com/joshii-h/blueferry-plugin-calendar",
    "ref": None, "capabilities": ["calendar"], "icon": "view-calendar",
}
DEEP_INDEX = b'{"version":1,"plugins":' + b"[" * 100_000 + b"]" * 100_000 + b"}"


def _index(*plugins) -> bytes:
    return json.dumps({"version": 1, "plugins": list(plugins)}).encode()


def test_entries_are_validated_field_by_field() -> None:
    entry = parse_entry(IMMICH)
    assert entry is not None and entry.available and entry.capabilities == ("photos",)
    soon = parse_entry(SOON)
    assert soon is not None and not soon.available
    for change in (
        {"repo": "http://github.com/x/y"}, {"repo": "git@github.com:x/y"},
        {"repo": "ext::sh -c id"}, {"id": "nope"}, {"name": ""}, {"ref": "--upload-pack=x"},
        {"ref": "a..b"}, {"api_version": "1"}, {"capabilities": "photos"},
    ):
        assert parse_entry({**IMMICH, **change}) is None, change
    odd = parse_entry({**IMMICH, "name": "<b>Imm\x1bich</b>\n", "icon": "../evil",
                       "screenshot": "javascript:alert(1)", "min_blueferry": "x",
                       "capabilities": ["photos", "BAD CAP", 3]})
    assert odd is not None
    assert odd.name == "<b>Imm ich</b>" and odd.icon == "" and odd.screenshot == ""
    assert odd.min_blueferry == "" and odd.capabilities == ("photos",)


def test_index_documents() -> None:
    entries = parse_index(_index(IMMICH, SOON, IMMICH, {"id": "broken"}))
    assert [entry.id for entry in entries] == [IMMICH["id"], SOON["id"]]
    for data in (b"[]", b"nope", DEEP_INDEX,
                 json.dumps({"version": 2, "plugins": []}).encode(),
                 b"x" * (MAX_INDEX_BYTES + 1)):
        with pytest.raises(IndexFetchError):
            parse_index(data)


@given(st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=30),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=10), children, max_size=6),
    max_leaves=30,
))
def test_parser_only_raises_its_own_error(document) -> None:
    for data in (json.dumps(document).encode(),
                 json.dumps({"version": 1, "plugins": [document]}).encode()):
        try:
            parse_index(data)
        except IndexFetchError:
            pass


class _Fetch:
    def __init__(self, data: bytes | Exception) -> None:
        self.data, self.calls = data, []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def test_cache_ttl_and_offline_fallback(tmp_path) -> None:
    now = [10_000.0]
    fetch = _Fetch(_index(IMMICH))
    index = PluginIndex(fetch=fetch, cache=tmp_path, clock=lambda: now[0], ttl=60)
    # mtime is real time; make the clock agree with it
    entries, note = index.load(DEFAULT_INDEX_URL)
    cached = next(tmp_path.iterdir())
    now[0] = cached.stat().st_mtime + 1
    assert note == "" and len(entries) == 1 and len(fetch.calls) == 1
    index.load(DEFAULT_INDEX_URL)
    assert len(fetch.calls) == 1  # fresh cache
    now[0] += 120
    fetch.data = IndexFetchError("could not fetch the index (URLError)")
    entries, note = index.load(DEFAULT_INDEX_URL)
    assert len(entries) == 1 and "cached" in note and len(fetch.calls) == 2
    with pytest.raises(IndexFetchError):
        PluginIndex(fetch=fetch, cache=tmp_path / "empty").load(DEFAULT_INDEX_URL)


def test_catalog_merges_indexes_and_marks_install_state(tmp_path) -> None:
    second = "https://example.org/more.json"
    fetches = {DEFAULT_INDEX_URL: _index(IMMICH, SOON),
               second: _index({**IMMICH, "name": "Shadowed"},
                              {**SOON, "id": "io.x.future", "api_version": 9, "ref": "v1"})}
    def fetch(url: str) -> bytes:
        if url not in fetches:
            raise IndexFetchError("could not fetch the index (URLError)")
        return fetches[url]

    index = PluginIndex(fetch=fetch, cache=tmp_path)
    record = InstallRecord(IMMICH["id"], IMMICH["repo"], "v0.1.0", "a" * 40, "/v")
    catalog = index.catalog(
        [DEFAULT_INDEX_URL, second, "https://broken.example/x"],
        installed={IMMICH["id"]: "0.1.0"}, records={IMMICH["id"]: record},
        blueferry_version="0.8.1", supported_api=frozenset({1}),
    )
    by_id = {item.entry.id: item for item in catalog.items}
    assert by_id[IMMICH["id"]].entry.name == "Immich photos"
    assert by_id[IMMICH["id"]].installed and by_id[IMMICH["id"]].update_available
    assert by_id[IMMICH["id"]].installed_ref == "v0.1.0"
    assert not by_id[SOON["id"]].entry.available
    assert not by_id["io.x.future"].compatible
    assert [url for url, _problem in catalog.problems] == ["https://broken.example/x"]
    assert [item.entry.id for item in search(catalog.items, "PHOTOS")] == [IMMICH["id"]]


def test_excessively_nested_response_preserves_cached_catalog(tmp_path) -> None:
    assert len(DEEP_INDEX) < MAX_INDEX_BYTES
    fetch = _Fetch(_index(IMMICH))
    index = PluginIndex(fetch=fetch, cache=tmp_path)
    index.load(DEFAULT_INDEX_URL)
    fetch.data = DEEP_INDEX
    entries, note = index.load(DEFAULT_INDEX_URL, refresh=True)
    assert [entry.id for entry in entries] == [IMMICH["id"]]
    assert "cached" in note and "not valid JSON" in note
    # A bad refresh must not overwrite the known-good cache.
    assert index.load(DEFAULT_INDEX_URL)[1] == ""


def test_excessively_nested_cache_is_replaced_from_network(tmp_path) -> None:
    fetch = _Fetch(_index(IMMICH))
    index = PluginIndex(fetch=fetch, cache=tmp_path)
    index.load(DEFAULT_INDEX_URL)
    cached = next(tmp_path.iterdir())
    cached.write_bytes(DEEP_INDEX)
    entries, note = index.load(DEFAULT_INDEX_URL)
    assert [entry.id for entry in entries] == [IMMICH["id"]] and not note
    assert len(fetch.calls) == 2
    assert cached.read_bytes() == _index(IMMICH)


def test_excessively_nested_catalog_does_not_block_other_indexes(tmp_path) -> None:
    good = "https://example.org/good.json"
    index = PluginIndex(
        fetch=lambda url: _index(IMMICH) if url == good else DEEP_INDEX, cache=tmp_path,
    )
    catalog = index.catalog(
        [DEFAULT_INDEX_URL, good], installed={}, records={},
        blueferry_version="0.9.0", supported_api=frozenset({1}),
    )
    assert [item.entry.id for item in catalog.items] == [IMMICH["id"]]
    assert catalog.problems == ((DEFAULT_INDEX_URL, "the index is not valid JSON"),)


def test_index_urls_from_settings() -> None:
    assert index_urls({}) == [DEFAULT_INDEX_URL]
    assert index_urls({"indexes": ["http://x", "https://a.example/i.json"]}) == [
        "https://a.example/i.json"]
    assert index_urls({"indexes": []}) == []


class _Response:
    def __init__(self, url: str, body: bytes) -> None:
        self.url, self.body = url, body

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _Opener:
    def __init__(self, response) -> None:
        self.response = response
        self.requests: list = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_fetch_refuses_a_final_url_off_https_and_oversized_indexes() -> None:
    from blueferry import plugin_index

    url = "https://example.org/index.json"
    opener = _Opener(_Response("http://example.org/index.json", b"{}"))
    with pytest.raises(IndexFetchError, match="away from https"):
        plugin_index._https_fetch(url, opener=lambda: opener)
    big = _Opener(_Response(url, b"x" * (plugin_index.MAX_INDEX_BYTES + 10)))
    with pytest.raises(IndexFetchError, match="too large"):
        plugin_index._https_fetch(url, opener=lambda: big)
    ok = _Opener(_Response(url, b'{"version": 1}'))
    assert plugin_index._https_fetch(url, opener=lambda: ok) == b'{"version": 1}'
    with pytest.raises(InstallError):
        plugin_index._https_fetch("http://example.org/index.json", opener=lambda: ok)


@pytest.mark.parametrize("target", [
    "http://example.org/index.json", "file:///etc/passwd", "ftp://example.org/x",
])
def test_every_redirect_hop_must_stay_on_https(target) -> None:
    """https -> http -> https would pass a check of the final URL only."""
    import urllib.request
    from email.message import Message

    from blueferry import plugin_index

    handler = plugin_index.HttpsOnlyRedirects()
    request = urllib.request.Request("https://example.org/index.json")
    with pytest.raises(plugin_index._InsecureRedirect):
        handler.redirect_request(request, None, 302, "Found", Message(), target)
    hop = handler.redirect_request(request, None, 302, "Found", Message(),
                                   "https://mirror.example.org/index.json")
    assert hop.full_url == "https://mirror.example.org/index.json"
    opener = _Opener(plugin_index._InsecureRedirect("the index redirected away from https"))
    with pytest.raises(IndexFetchError, match="away from https"):
        plugin_index._https_fetch("https://example.org/index.json", opener=lambda: opener)


def test_the_default_opener_uses_the_https_only_redirects() -> None:
    from blueferry import plugin_index

    handlers = plugin_index._opener().handlers
    assert any(isinstance(h, plugin_index.HttpsOnlyRedirects) for h in handlers)
    assert plugin_index.HttpsOnlyRedirects.max_redirections <= 5
