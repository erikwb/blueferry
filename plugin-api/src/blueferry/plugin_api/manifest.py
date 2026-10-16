"""Plugin manifests: desktop-entry-like ``*.plugin`` files.

Example ``~/.local/share/blueferry/plugins/io.example.photos.plugin``::

    [BlueFerry Plugin]
    Id=io.example.photos
    Name=Example photos
    Version=1.0.0
    ApiVersion=1
    MinBlueFerry=0.8
    Capabilities=photos;
    Homepage=https://example.org/photos
    Source=https://git.example.org/photos.git
    Exec=blueferry-plugin-example serve
    Cli=blueferry-plugin-example
    Alias=example

    [Config url]
    Label=Server URL
    Type=url
    Required=true
    Placeholder=https://photos.example.org

Discovery never runs anything; it only parses files. A manifest that is
malformed, too large or written for an unsupported ``ApiVersion`` is ignored
with a reason the clients can show.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    API_MINOR,
    BUS_NAME_PREFIX,
    KNOWN_CAPABILITIES,
    KNOWN_TOOLS,
    SUPPORTED_API_VERSIONS,
)
from .config import ACTION_PREFIX as CONFIG_ACTION_PREFIX
from .config import GROUP_PREFIX as CONFIG_GROUP_PREFIX
from .config import SECTION_PREFIX as CONFIG_SECTION_PREFIX
from .config import (
    ConfigAction,
    ConfigField,
    ConfigGroup,
    form_groups,
    parse_actions,
    parse_fields,
    parse_groups,
)

GROUP = "BlueFerry Plugin"
SUFFIX = ".plugin"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_MANIFESTS_PER_DIRECTORY = 64
# Reverse-DNS: at least two dot-separated elements of [A-Za-z0-9_-], none
# starting with a digit, so the derived bus name is valid.
_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+$")
_ALIAS = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION = re.compile(r"^\d+(\.\d+){0,3}([-+.~][0-9A-Za-z.]+)?$")
_URL = re.compile(r"^https://[^\s/]+(/\S*)?$")
_API_VERSION = re.compile(r"^(\d{1,3})(?:\.(\d{1,3}))?$")
# ConfigLogin names a browser sign-in flow; clients show a button only for
# providers they know (see LOGIN_PROVIDERS) and ignore others.
_LOGIN_PROVIDER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REQUIRED = (
    "Id", "Name", "Version", "ApiVersion", "MinBlueFerry", "Capabilities", "Exec",
)


class ManifestError(ValueError):
    """A manifest that clients must ignore; the message says why."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: int
    min_blueferry: str
    capabilities: tuple[str, ...]
    exec: tuple[str, ...]
    homepage: str = ""
    source: str = ""
    cli: tuple[str, ...] = ()
    alias: str = ""
    path: Path | None = None
    # Optional settings form (``[Config <key>]`` groups), in manifest order.
    config: tuple[ConfigField, ...] = ()
    # Minor revision of the contract (``ApiVersion=1.2``); at most API_MINOR.
    api_minor: int = 0
    # ApiVersion 1.3: the form's sections in display order, whether the
    # plugin implements TestConfig, and its ConfigLogin provider (or "").
    config_groups: tuple[ConfigGroup, ...] = ()
    config_test: bool = False
    config_login: str = ""
    # ApiVersion 1.4: companion tools (KNOWN_TOOLS) this plugin replaces.
    replaces_tools: tuple[str, ...] = ()
    # ApiVersion 1.6: buttons of the settings form (``[ConfigAction <id>]``).
    config_actions: tuple[ConfigAction, ...] = ()

    def config_action(self, action_id: str) -> ConfigAction | None:
        """The declared settings action with this id, else None."""
        return next((action for action in self.config_actions if action.id == action_id), None)

    @property
    def bus_name(self) -> str:
        return bus_name_for(self.id)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class Discovery:
    plugins: tuple[PluginManifest, ...] = ()
    # (file name, reason) for every manifest that was skipped.
    ignored: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def with_capability(self, capability: str) -> tuple[PluginManifest, ...]:
        return tuple(plugin for plugin in self.plugins if plugin.has(capability))

    def find(self, name: str) -> PluginManifest | None:
        for plugin in self.plugins:
            if name in (plugin.id, plugin.alias):
                return plugin
        return None


def valid_plugin_id(plugin_id: object) -> bool:
    """A reverse-DNS plugin id like ``io.example.photos`` (at most 120 characters)."""
    return isinstance(plugin_id, str) and len(plugin_id) <= 120 and bool(
        _ID.fullmatch(plugin_id))


def bus_name_for(plugin_id: str) -> str:
    """``io.weirdware.BlueFerry.Plugin.<id>``; '-' is not valid in names."""
    return BUS_NAME_PREFIX + plugin_id.replace("-", "_")


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+){0,3})", value.strip())
    if match is None:
        raise ManifestError(f"not a version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


_Groups = list[tuple[str, dict[str, str]]]


def _keyfile(text: str) -> tuple[dict[str, str], _Groups, _Groups, _Groups]:
    """The ``[BlueFerry Plugin]`` keys, the ``[Config <key>]`` groups, the
    ``[ConfigGroup <name>]`` sections and the ``[ConfigAction <id>]``
    buttons. Other groups are skipped, so older clients ignore newer ones."""
    values: dict[str, str] = {}
    config: _Groups = []
    sections: _Groups = []
    actions: _Groups = []
    target: dict[str, str] | None = None
    group = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            if group == GROUP:
                target = values
            elif group.startswith(CONFIG_GROUP_PREFIX):
                target = {}
                config.append((group[len(CONFIG_GROUP_PREFIX):].strip(), target))
            elif group.startswith(CONFIG_SECTION_PREFIX):
                target = {}
                sections.append((group[len(CONFIG_SECTION_PREFIX):].strip(), target))
            elif group.startswith(CONFIG_ACTION_PREFIX):
                target = {}
                actions.append((group[len(CONFIG_ACTION_PREFIX):].strip(), target))
            else:
                target = None
            continue
        if target is None:
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or "[" in key:
            # Localized keys (Name[de]) are allowed and ignored for now.
            if "[" in key:
                continue
            raise ManifestError(f"malformed line: {raw[:40]!r}")
        if key in target:
            raise ManifestError(f"duplicate key {key}")
        target[key] = value.strip()
    if group is None and not values:
        raise ManifestError(f"missing [{GROUP}] group")
    return values, config, sections, actions


def _config_flag(value: str, key: str) -> bool:
    lowered = value.strip().casefold()
    if lowered in ("", "false", "no", "0", "off"):
        return False
    if lowered in ("true", "yes", "1", "on"):
        return True
    raise ManifestError(f"{key} must be true or false")


def _argv(value: str, key: str) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(value))
    except ValueError as error:
        raise ManifestError(f"{key} is not a valid command line") from error
    if not argv:
        raise ManifestError(f"{key} is empty")
    if any("\x00" in part or "\n" in part for part in argv):
        raise ManifestError(f"{key} contains control characters")
    return argv


def parse_manifest(
    text: str,
    *,
    path: Path | None = None,
    blueferry_version: str | None = None,
) -> PluginManifest:
    """Parse and validate one manifest; raise ManifestError to skip it."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is too large")
    values, config_groups, config_sections, config_action_sections = _keyfile(text)
    missing = [key for key in _REQUIRED if not values.get(key)]
    if missing:
        raise ManifestError("missing " + ", ".join(missing))
    plugin_id = values["Id"]
    if not valid_plugin_id(plugin_id):
        raise ManifestError("Id must be a reverse-DNS name like io.example.photos")
    api_match = _API_VERSION.fullmatch(values["ApiVersion"])
    if api_match is None:
        raise ManifestError("ApiVersion must be MAJOR or MAJOR.MINOR")
    api_version = int(api_match.group(1))
    api_minor = int(api_match.group(2) or 0)
    if api_version not in SUPPORTED_API_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_API_VERSIONS))
        raise ManifestError(
            f"written for plugin API {api_version}; this BlueFerry supports {supported}"
        )
    if api_minor > API_MINOR:
        # 1.x is accepted when this BlueFerry knows at least that minor.
        raise ManifestError(
            f"needs plugin API {api_version}.{api_minor}; this BlueFerry supports "
            f"{api_version}.{API_MINOR}"
        )
    for key in ("Version", "MinBlueFerry"):
        if len(values[key]) > 40 or not _VERSION.fullmatch(values[key]):
            raise ManifestError(f"{key} is not a version")
    if blueferry_version is not None and version_tuple(blueferry_version) < version_tuple(
        values["MinBlueFerry"]
    ):
        raise ManifestError(f"needs BlueFerry {values['MinBlueFerry']} or newer")
    capabilities = tuple(
        dict.fromkeys(part.strip() for part in values["Capabilities"].split(";") if part.strip())
    )
    usable = tuple(c for c in capabilities if c in KNOWN_CAPABILITIES)
    if not usable:
        raise ManifestError("no capability this BlueFerry understands")
    for key in ("Homepage", "Source"):
        if values.get(key) and (len(values[key]) > 300 or not _URL.fullmatch(values[key])):
            raise ManifestError(f"{key} must be an https URL")
    if not (values.get("Homepage") or values.get("Source")):
        raise ManifestError("missing Homepage or Source")
    alias = values.get("Alias", "")
    if alias and not _ALIAS.fullmatch(alias):
        raise ManifestError("Alias must be a short lowercase word")
    name = values["Name"]
    if len(name) > 80 or any(ord(ch) < 32 for ch in name):
        raise ManifestError("Name is too long or contains control characters")
    try:
        sections = parse_groups(config_sections)
        config = parse_fields(config_groups, sections)
        config_actions = parse_actions(config_action_sections)
    except ValueError as error:
        raise ManifestError(str(error)) from None
    config_test = _config_flag(values.get("ConfigTest", ""), "ConfigTest")
    config_login = values.get("ConfigLogin", "").strip()
    if config_login and not _LOGIN_PROVIDER.fullmatch(config_login):
        raise ManifestError("ConfigLogin must be a short lowercase provider name")
    if (config_test or config_login) and not config:
        raise ManifestError("ConfigTest and ConfigLogin need [Config …] settings")
    if config_actions and not config:
        # The buttons live in the settings form, which needs settings.
        raise ManifestError("[ConfigAction …] needs [Config …] settings")
    replaces = []
    for part in values.get("ReplacesTools", "").split(";"):
        tool = part.strip()
        if not tool:
            continue
        if not _LOGIN_PROVIDER.fullmatch(tool):
            raise ManifestError("ReplacesTools must list short lowercase tool names")
        if tool in KNOWN_TOOLS and tool not in replaces:
            replaces.append(tool)
    return PluginManifest(
        id=plugin_id,
        name=name,
        version=values["Version"],
        api_version=api_version,
        min_blueferry=values["MinBlueFerry"],
        capabilities=usable,
        exec=_argv(values["Exec"], "Exec"),
        homepage=values.get("Homepage", ""),
        source=values.get("Source", ""),
        cli=_argv(values["Cli"], "Cli") if values.get("Cli") else (),
        alias=alias,
        path=path,
        config=config,
        api_minor=api_minor,
        config_groups=form_groups(config, sections),
        config_test=config_test,
        config_login=config_login,
        replaces_tools=tuple(replaces),
        config_actions=config_actions,
    )


def default_directories() -> tuple[Path, ...]:
    """User manifests first: they shadow system ones with the same Id."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    roots = [data_home, *[part for part in data_dirs.split(":") if part]]
    return tuple(dict.fromkeys(Path(root) / "blueferry" / "plugins" for root in roots))


def _read_manifest(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode):
            raise ManifestError("not a regular file")
        # Another user's writable file could point clients at any program.
        if current.st_uid not in (0, os.getuid()) or current.st_mode & 0o022:
            raise ManifestError("owned or writable by another user")
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("manifest is not UTF-8") from None


def discover(
    directories: Sequence[Path] | None = None,
    *,
    blueferry_version: str | None = None,
) -> Discovery:
    """Parse every ``*.plugin`` file; the first manifest per Id wins."""
    plugins: dict[str, PluginManifest] = {}
    ignored: list[tuple[str, str]] = []
    for directory in directories if directories is not None else default_directories():
        for path in _manifest_files(directory):
            try:
                manifest = parse_manifest(
                    _read_manifest(path), path=path, blueferry_version=blueferry_version,
                )
            except (ManifestError, OSError) as error:
                ignored.append((path.name, str(error) or type(error).__name__))
                continue
            plugins.setdefault(manifest.id, manifest)
    return Discovery(tuple(plugins.values()), tuple(ignored))


def _manifest_files(directory: Path) -> Iterable[Path]:
    try:
        names = sorted(
            entry.name for entry in os.scandir(directory)
            if entry.name.endswith(SUFFIX) and not entry.name.startswith(".")
        )
    except OSError:
        return ()
    return [directory / name for name in names[:MAX_MANIFESTS_PER_DIRECTORY]]
