"""blueferry.plugin_api: manifests, the validating client, the service base."""
from __future__ import annotations

import ast
import json
import os
import threading
import time
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blueferry.plugin_api import API_MINOR, MAX_REPLY_BYTES
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.manifest import (
    ManifestError,
    bus_name_for,
    discover,
    parse_manifest,
)
from blueferry.plugin_api.service import PhotosService, RateLimitedError
from blueferry.plugin_api.testing import (
    ScriptedTransport,
    ServiceTransport,
    inline_service,
    manifest,
)

VALID = """# comment
[Desktop Entry]
Name=ignored
[BlueFerry Plugin]
Id=io.example.immich-photos
Name=Immich photos
Name[de]=Immich-Fotos
Version=0.1.0
ApiVersion=1
MinBlueFerry=0.8
Capabilities=photos;future-thing;
Homepage=https://example.org/immich
Source=https://git.example.org/immich.git
Exec=python3 -m example_plugin serve
Cli=python3 -m example_plugin
Alias=immich
"""


def test_manifest_parses_required_and_optional_keys() -> None:
    parsed = parse_manifest(VALID, blueferry_version="0.8.1")
    assert parsed.id == "io.example.immich-photos"
    assert parsed.bus_name == "io.weirdware.BlueFerry.Plugin.io.example.immich_photos"
    assert parsed.capabilities == ("photos",)
    assert parsed.exec == ("python3", "-m", "example_plugin", "serve")
    assert parsed.cli[-1] == "example_plugin" and parsed.alias == "immich"


@pytest.mark.parametrize("change,reason", [
    (("ApiVersion=1", "ApiVersion=2"), "plugin API 2"),
    (("ApiVersion=1", "ApiVersion=x"), "MAJOR"),
    (("MinBlueFerry=0.8", "MinBlueFerry=9.0"), "needs BlueFerry 9.0"),
    (("Id=io.example.immich-photos", "Id=photos"), "reverse-DNS"),
    (("Id=io.example.immich-photos", "Id=io.9bad.x"), "reverse-DNS"),
    (("Capabilities=photos;future-thing;", "Capabilities=nope;"), "no capability"),
    (("Homepage=https://example.org/immich", "Homepage=http://example.org"), "https"),
    (("Exec=python3 -m example_plugin serve", "Exec='unterminated"), "Exec"),
    (("Alias=immich", "Alias=Bad Alias"), "Alias"),
    (("Version=0.1.0\n", ""), "missing Version"),
])
def test_manifest_rejections_say_why(change, reason) -> None:
    with pytest.raises(ManifestError, match=reason):
        parse_manifest(VALID.replace(*change), blueferry_version="0.8.1")


def test_manifest_needs_a_homepage_or_source() -> None:
    text = VALID.replace("Homepage=https://example.org/immich\n", "").replace(
        "Source=https://git.example.org/immich.git\n", "")
    with pytest.raises(ManifestError, match="Homepage or Source"):
        parse_manifest(text)


@given(st.text(max_size=2000))
def test_manifest_parser_only_raises_manifest_errors(text) -> None:
    try:
        parse_manifest(text)
    except ManifestError:
        pass


def test_bus_name_replaces_hyphens() -> None:
    assert bus_name_for("io.a.b-c") == "io.weirdware.BlueFerry.Plugin.io.a.b_c"


def _write(directory: Path, name: str, text: str, mode: int = 0o644) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    path.chmod(mode)
    return path


def test_discovery_prefers_user_manifests_and_reports_ignored(tmp_path) -> None:
    user, system = tmp_path / "user", tmp_path / "system"
    _write(user, "a.plugin", VALID.replace("Name=Immich photos", "Name=Mine"))
    _write(system, "a.plugin", VALID)
    _write(system, "b.plugin", VALID.replace("ApiVersion=1", "ApiVersion=7"))
    _write(system, "c.plugin", VALID.replace("io.example.immich", "io.example.other"), 0o666)
    _write(system, "notes.txt", "x")
    found = discover([user, system, tmp_path / "missing"], blueferry_version="0.8.1")
    assert [plugin.name for plugin in found.plugins] == ["Mine"]
    reasons = dict(found.ignored)
    assert "plugin API 7" in reasons["b.plugin"]
    assert "another user" in reasons["c.plugin"]
    assert found.find("immich") is found.plugins[0]
    assert found.with_capability("photos") == found.plugins


# ---- client validation ---------------------------------------------------------


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "cache" / "blueferry"
    (root / "immich").mkdir(parents=True)
    return root


def _client(replies, cache, **kwargs) -> PluginClient:
    return PluginClient(
        manifest(), transport=ScriptedTransport(replies, **kwargs), cache_root=lambda: cache,
    )


def test_list_recent_keeps_only_valid_entries_and_own_cache_files(cache, tmp_path) -> None:
    thumb = cache / "immich" / "a.webp"
    thumb.write_bytes(b"x")
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"x")
    escape = cache / "immich" / "escape.webp"
    escape.symlink_to(outside)
    reply = json.dumps([
        {"id": "a", "taken_at": "2026-10-06T18:21:00Z", "type": "image", "thumbnail": str(thumb)},
        {"id": "b", "taken_at": "<b>x</b>", "type": "evil", "thumbnail": str(outside)},
        {"id": "c", "taken_at": "2026-10-05", "type": "video", "thumbnail": str(escape)},
        {"id": "../etc", "type": "image"},
        {"id": "a", "type": "image"},
        "junk",
    ])
    photos = _client({"ListRecent": reply}, cache).list_recent(500)
    assert [photo.id for photo in photos] == ["a", "b", "c"]
    assert photos[0].thumbnail == thumb.resolve()
    assert photos[1].taken_at == "" and photos[1].type == "other"
    assert photos[1].thumbnail is None and photos[2].thumbnail is None


@pytest.mark.parametrize("reply,message", [
    ("x" * (MAX_REPLY_BYTES + 1), "oversized"),
    ("{not json", "invalid JSON"),
    (7, "invalid reply"),
    ('{"a": 1}', "invalid photo list"),
])
def test_bad_replies_are_errors_not_crashes(cache, reply, message) -> None:
    with pytest.raises(PluginError, match=message):
        _client({"ListRecent": reply}, cache).list_recent(5)


def test_a_plugin_owned_by_another_user_is_refused(cache) -> None:
    with pytest.raises(PluginError, match="different user"):
        _client({"Status": '{"state": "ok"}'}, cache, uid=os.getuid() + 1).status()


def test_status_and_fetch_original_are_validated(cache) -> None:
    original = cache / "immich" / "IMG_0001.HEIC"
    original.write_bytes(b"x")
    client = _client({
        "Status": json.dumps({"state": "unconfigured", "detail": "run\x1b[31m setup"}),
        "FetchOriginal": str(original),
    }, cache)
    status = client.status()
    assert status.state == "unconfigured" and "\x1b" not in status.detail
    assert client.fetch_original("abc") == original.resolve()
    with pytest.raises(PluginError):
        client.fetch_original("../x")
    with pytest.raises(PluginError, match="invalid status"):
        _client({"Status": '{"state": "pwned"}'}, cache).status()


# ---- service base --------------------------------------------------------------


class _Photos(PhotosService):
    def __init__(self, *args, files=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.files = files or {}

    def status(self) -> dict[str, object]:
        return {"state": "ok", "server": "photos.example.org"}

    def list_recent(self, limit):
        return [{"id": key, "taken_at": "2026-10-06", "type": "image", "thumbnail": path}
                for key, path in list(self.files.items())[:limit]]

    def fetch_original(self, photo_id):
        if photo_id not in self.files:
            raise KeyError(photo_id)  # never leaks into the reply text
        return self.files[photo_id]


def test_service_round_trip_through_the_harness(cache) -> None:
    path = cache / "immich" / "one.jpg"
    path.write_bytes(b"x")
    service = inline_service(_Photos, manifest(), files={"one": str(path)})
    client = PluginClient(manifest(), transport=ServiceTransport(service),
                          cache_root=lambda: cache)
    assert client.status().server == "photos.example.org"
    assert [photo.id for photo in client.list_recent(10)] == ["one"]
    assert client.fetch_original("one") == path.resolve()
    with pytest.raises(PluginError, match="KeyError"):
        client.fetch_original("two")
    info = json.loads(service.GetInfo(sender=":1.9"))
    assert info["api_version"] == 1 and info["capabilities"] == ["photos"]


def test_service_rate_limits_a_runaway_caller() -> None:
    now = [0.0]
    service = inline_service(_Photos, manifest(), clock=lambda: now[0])
    for _ in range(120):
        service.Status(sender=":1.5")
    with pytest.raises(RateLimitedError):
        service.Status(sender=":1.5")
    service.Status(sender=":1.6")
    now[0] = 61.0
    service.Status(sender=":1.5")


@pytest.mark.private_dbus
def test_service_answers_on_the_private_bus(cache) -> None:
    import dbus
    import dbus.mainloop
    import dbus.service
    from gi.repository import GLib

    from blueferry.plugin_api.client import DBusTransport
    from tests.private_bus import open_private_bus

    path = cache / "immich" / "p.jpg"
    path.write_bytes(b"x")
    plugin = manifest(f"io.example.t{os.getpid()}")
    bus = dbus.SessionBus()
    name = dbus.service.BusName(plugin.bus_name, bus=bus, do_not_queue=True)
    service = _Photos(plugin, bus, files={"p": str(path)}, start_worker=lambda work: work())
    outcome: dict = {}

    def request() -> None:
        connection = open_private_bus(mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = PluginClient(plugin, transport=DBusTransport(connection),
                                  cache_root=lambda: cache)
            outcome["photos"] = client.list_recent(5)
            outcome["status"] = client.status()
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=request)
    thread.start()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    thread.join(1)
    try:
        assert "error" not in outcome, outcome
        assert outcome["photos"][0].thumbnail == path.resolve()
        assert outcome["status"].ready
    finally:
        service.remove_from_connection()
        del name
        bus.release_name(plugin.bus_name)


def test_plugin_api_imports_nothing_else_from_blueferry() -> None:
    """The package must stay separable into its own distribution."""
    root = Path(__file__).resolve().parents[1] / "src" / "blueferry" / "plugin_api"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").startswith("blueferry"), (path.name, node.module)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("blueferry") for a in node.names), path.name


def test_plugin_api_ships_from_one_tree() -> None:
    """BlueFerry and blueferry-plugin-api build the same files, not copies."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    link = root / "src" / "blueferry" / "plugin_api"
    assert link.is_symlink()
    assert link.resolve() == (root / "plugin-api" / "src" / "blueferry" / "plugin_api").resolve()
    # A blueferry/__init__.py there would replace BlueFerry's own when both
    # distributions share an environment.
    assert not (root / "plugin-api" / "src" / "blueferry" / "__init__.py").exists()
    with (root / "plugin-api" / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["name"] == "blueferry-plugin-api"
    assert project["tool"]["setuptools"]["packages"]["find"]["namespaces"] is True


# ---- settings schema (Config groups, GetConfig/SetConfig) ------------------------

CONFIG = """
[Config url]
Label=Server URL
Type=url
Required=true
Help=For example https://photos.example.org

[Config api_key]
Label=API key
Type=secret
Required=true

[Config camera_model]
Label=Camera model
Type=string

[Config size]
Label=Size
Type=choice
Choices=small;large;
Default=small

[Config limit]
Label=Limit
Type=int
Min=1
Max=200
Default=60

[Config videos]
Label=Videos
Type=bool
Default=true
"""


def _configured(extra: str = CONFIG):
    return manifest(extra=extra)


def test_manifest_reads_the_settings_schema_in_order() -> None:
    parsed = parse_manifest(VALID.replace("ApiVersion=1", "ApiVersion=1.1") + CONFIG)
    assert parsed.api_version == 1 and parsed.api_minor == 1
    assert [field.key for field in parsed.config] == [
        "url", "api_key", "camera_model", "size", "limit", "videos",
    ]
    url, key, _model, size, limit, videos = parsed.config
    assert url.required and url.type == "url" and url.help.startswith("For example")
    assert key.secret and size.choices == ("small", "large") and size.default == "small"
    assert (limit.minimum, limit.maximum, limit.default) == (1, 200, 60)
    assert videos.default is True
    assert parse_manifest(VALID).config == ()


@pytest.mark.parametrize("group,reason", [
    ("[Config Bad]\nLabel=x\nType=string\n", "lowercase"),
    ("[Config a]\nLabel=x\nType=float\n", "unknown Type"),
    ("[Config a]\nType=string\n", "missing Label"),
    ("[Config a]\nLabel=x\nType=secret\nDefault=hunter2\n", "secret cannot"),
    ("[Config a]\nLabel=x\nType=choice\n", "Choices"),
    ("[Config a]\nLabel=x\nType=int\nMin=a\n", "integers"),
    ("[Config a]\nLabel=x\nType=int\nMax=3\nDefault=9\n", "invalid Default"),
    ("[Config a]\nLabel=x\nType=bool\nRequired=maybe\n", "Required"),
    ("[Config a]\nLabel=x\nType=string\n[Config a]\nLabel=y\nType=string\n", "duplicate"),
])
def test_bad_settings_schema_ignores_the_manifest(group, reason) -> None:
    with pytest.raises(ManifestError, match=reason):
        parse_manifest(VALID + group)


def test_validate_merges_checks_and_keeps_stored_secrets() -> None:
    from blueferry.plugin_api.config import SECRET_MASK, validate

    fields = _configured().config
    current = {"url": "https://a.example", "api_key": True, "limit": 10}
    values, errors = validate(fields, current, {"limit": 20, "api_key": SECRET_MASK})
    assert errors == {}
    assert values["url"] == "https://a.example" and values["limit"] == 20
    assert "api_key" not in values and values["size"] == "small" and values["videos"] is True
    values, errors = validate(fields, {}, {
        "url": "http://evil.example", "limit": 500, "size": "huge", "videos": "yes",
        "bogus": 1,
    })
    assert set(errors) == {"url", "api_key", "limit", "size", "videos", "bogus"}
    values, errors = validate(fields, {}, {"url": "http://localhost:2283/", "api_key": "k"})
    assert errors == {} and values["url"] == "http://localhost:2283" and values["api_key"] == "k"


@given(st.dictionaries(st.text(max_size=8), st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=40),
    st.lists(st.integers(), max_size=3),
), max_size=8))
def test_validate_never_raises(update) -> None:
    from blueferry.plugin_api.config import validate

    values, errors = validate(_configured().config, {}, update)
    assert isinstance(values, dict) and isinstance(errors, dict)


class _Configurable(_Photos):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stored: dict[str, object] = {"url": "https://a.example", "api_key": "s3cret"}

    def config_values(self):
        return dict(self.stored)

    def apply_config(self, values) -> None:
        from blueferry.plugin_api.config import ConfigError

        if values.get("api_key") == "refused":
            raise ConfigError("api_key", "the server refused this key")
        self.stored.update(values)


def test_settings_round_trip_never_reveals_a_secret(cache) -> None:
    from blueferry.plugin_api.config import SECRET_MASK

    service = inline_service(_Configurable, _configured())
    client = PluginClient(_configured(), transport=ServiceTransport(service),
                          cache_root=lambda: cache)
    shown = client.get_config()
    assert shown["api_key"] == SECRET_MASK and shown["url"] == "https://a.example"
    assert "s3cret" not in json.dumps(shown)
    raw = []
    service.GetConfig(reply=raw.append, error=raw.append, sender=":1.2")
    assert "s3cret" not in raw[0]
    result = client.set_config({"url": "https://b.example", "api_key": SECRET_MASK, "x": 1})
    assert result.ok and service.stored["api_key"] == "s3cret"
    assert service.stored["url"] == "https://b.example"
    result = client.set_config({"api_key": "refused"})
    assert not result.ok and result.errors == {"api_key": "the server refused this key"}
    result = client.set_config({"url": "ftp://nope"})
    assert not result.ok and "url" in result.errors
    info = json.loads(service.GetInfo(sender=":1.9"))
    assert info["api_minor"] == API_MINOR


def test_settings_are_refused_without_a_schema_and_bad_replies_are_errors(cache) -> None:
    service = inline_service(_Configurable, manifest())
    client = PluginClient(manifest(), transport=ServiceTransport(service),
                          cache_root=lambda: cache)
    with pytest.raises(PluginError, match="no settings"):
        client.get_config()
    outcome = []
    service.SetConfig("{}", reply=outcome.append, error=outcome.append, sender=":1.2")
    assert "no settings" in str(outcome[0])
    for reply in ("[]", '{"values": 3}', "nope"):
        scripted = PluginClient(_configured(), transport=ScriptedTransport({"GetConfig": reply}),
                                cache_root=lambda: cache)
        with pytest.raises(PluginError):
            scripted.get_config()
    leaky = PluginClient(_configured(), transport=ScriptedTransport(
        {"GetConfig": json.dumps({"values": {"api_key": "plain", "limit": "x"}}),
         "SetConfig": json.dumps({"ok": False})},
    ), cache_root=lambda: cache)
    shown = leaky.get_config()
    assert shown["api_key"] != "plain" and shown["limit"] == 60
    assert leaky.set_config({}).errors == {"": "the plugin rejected the settings"}
