"""Plugin settings: the manifest's ``[Config <key>]`` groups and their values.

A plugin describes its settings in the manifest; clients build a form from
that schema, read the current values with ``GetConfig()`` and write changes
with ``SetConfig()``. Validation runs in the plugin (:func:`validate`) and,
for a quick answer, in the client too. Secrets only travel *into* the
plugin: ``GetConfig()`` reports :data:`SECRET_MASK` for a stored secret and
an empty string for a missing one, and a client sends a secret only when the
user typed a new one.

Example manifest groups::

    [Config url]
    Label=Server URL
    Type=url
    Required=true
    Help=For example https://photos.example.org

    [Config api_key]
    Label=API key
    Type=secret
    Required=true

ApiVersion 1.3 adds optional presentation keys (``Placeholder``, ``Example``,
``HelpUrl``, ``Group``/``Advanced`` with ``[ConfigGroup <name>]`` sections),
a client-side pre-check (``Pattern``, ``Min``/``Max``, ``ErrorText``) and
conditional fields (``ShowIf=<key>=<value>``). Older clients ignore them; the
plugin's own :func:`validate` stays authoritative.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

GROUP_PREFIX = "Config "
SECTION_PREFIX = "ConfigGroup "
TYPES = frozenset({"string", "url", "secret", "bool", "int", "choice"})
SECRET_MASK = "********"
MAX_FIELDS = 24
MAX_CONFIG_BYTES = 16 * 1024
MAX_TEXT = 1024
MAX_URL = 2048
MAX_SECRET = 4096
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CHOICE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_URL = re.compile(r"^https://[^\s/?#]+(/\S*)?$")
_LOCAL_URL = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?(/\S*)?$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
# ApiVersion 1.3 limits for the presentation keys (plain text, one line).
MAX_LABEL = 80
MAX_HELP = 300
MAX_PLACEHOLDER = 120
MAX_EXAMPLE = 160
MAX_ERROR_TEXT = 160
MAX_PATTERN = 200
MAX_HELP_URL = 300
MAX_GROUP_LABEL = 40
MAX_GROUPS = 8
# Groups a field may name without declaring a [ConfigGroup] section.
ADVANCED = "advanced"
BUILTIN_GROUPS = {"account": "Account", "options": "Options", ADVANCED: "Advanced"}
# Pattern runs (in Python, also in the clients) on every key press, so it is
# limited to plain syntax that cannot backtrack badly: no groups with flags or
# lookarounds, no backreferences, no quantified groups holding a quantifier.
_PATTERN_FORBIDDEN = re.compile(r"\(\?|\\[1-9AZbBpPkgN]|\[\[|\(\)")
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+?}][^()]*\)\s*[*+{]")


class ConfigError(ValueError):
    """One setting is invalid; ``field`` names it (empty: the whole form)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class ConfigField:
    key: str
    label: str
    type: str
    required: bool = False
    default: object = None
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    # ApiVersion 1.3 presentation and pre-check keys; all optional.
    placeholder: str = ""
    example: str = ""
    help_url: str = ""
    group: str = ""
    pattern: str = ""
    error_text: str = ""
    # (key, value): shown only while that earlier field has this value.
    show_if: tuple[str, str] | None = None

    @property
    def secret(self) -> bool:
        return self.type == "secret"

    def empty(self) -> object:
        """The value of an unset field: its default, else a neutral value."""
        if self.default is not None:
            return self.default
        if self.type == "bool":
            return False
        if self.type == "int":
            return self.minimum if self.minimum is not None else 0
        return ""

    def coerce(self, value: object) -> object:
        """A valid value of this field's type; raise ConfigError otherwise."""
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            raise ConfigError(self.key, "must be on or off")
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(self.key, "must be a whole number")
            if self.minimum is not None and value < self.minimum:
                raise ConfigError(self.key, self.error_text or f"must be at least {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ConfigError(self.key, self.error_text or f"must be at most {self.maximum}")
            return value
        if not isinstance(value, str):
            raise ConfigError(self.key, "must be text")
        if any(not ch.isprintable() for ch in value):
            raise ConfigError(self.key, "contains control characters")
        text = value.strip()
        if self.type == "choice":
            if text not in self.choices:
                raise ConfigError(self.key, "is not one of the offered choices")
            return text
        if self.type == "url":
            if len(text) > MAX_URL or not (_URL.fullmatch(text) or _LOCAL_URL.fullmatch(text)):
                raise ConfigError(
                    self.key, "must be an https:// URL (http only for localhost)",
                )
            self._match(text)
            return text.rstrip("/")
        limit = MAX_SECRET if self.secret else MAX_TEXT
        if len(text) > limit:
            raise ConfigError(self.key, "is too long")
        if text:
            self._match(text)
        return text

    def _match(self, text: str) -> None:
        if self.pattern and re.fullmatch(self.pattern, text) is None:
            raise ConfigError(self.key, self.error_text or "does not have the expected format")

    def shown(self, values: Mapping[str, object]) -> bool:
        """Whether a ``ShowIf`` field applies to ``values`` (always without one)."""
        if self.show_if is None:
            return True
        key, wanted = self.show_if
        return value_text(values.get(key)) == wanted


@dataclass(frozen=True, slots=True)
class ConfigGroup:
    """A form section (``[ConfigGroup <name>]`` or a built-in name)."""

    name: str
    label: str
    help: str = ""
    collapsed: bool = False


def value_text(value: object) -> str:
    """How ``ShowIf`` compares a value: ``true``/``false``, numbers, text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip()


def visible_fields(
    fields: Sequence[ConfigField], values: Mapping[str, object],
) -> tuple[ConfigField, ...]:
    """The fields whose ``ShowIf`` holds, including the chain behind it."""
    shown: set[str] = set()
    result = []
    for field in fields:
        # ShowIf names an earlier field, so its visibility is already known.
        if field.show_if is not None and field.show_if[0] not in shown:
            continue
        if field.shown(values):
            shown.add(field.key)
            result.append(field)
    return tuple(result)


def _flag(value: str, key: str, name: str = "Required") -> bool:
    lowered = value.strip().casefold()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"Config {key}: {name} must be true or false")


# Bidi overrides could make a label read differently from what it is.
_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _plain(value: str, limit: int, what: str) -> str:
    if len(value) > limit or any(ord(ch) < 32 or ch == "\x7f" or ch in _BIDI for ch in value):
        raise ValueError(f"{what} is too long or contains control characters")
    return value


def _pattern(value: str, key: str) -> str:
    if not value:
        return ""
    what = f"Config {key}: Pattern"
    _plain(value, MAX_PATTERN, what)
    if _PATTERN_FORBIDDEN.search(value) or _NESTED_QUANTIFIER.search(value):
        raise ValueError(f"{what} uses syntax outside the simple subset")
    try:
        re.compile(value)
    except re.error:
        raise ValueError(f"{what} is not a regular expression") from None
    return value


def _help_url(value: str, key: str) -> str:
    if value and (len(value) > MAX_HELP_URL or not _URL.fullmatch(value)):
        raise ValueError(f"Config {key}: HelpUrl must be an https URL")
    return value


def parse_groups(
    sections: Sequence[tuple[str, Mapping[str, str]]],
) -> dict[str, ConfigGroup]:
    """``[ConfigGroup <name>]`` sections; built-in names may be relabelled."""
    if len(sections) > MAX_GROUPS:
        raise ValueError(f"more than {MAX_GROUPS} config groups")
    groups: dict[str, ConfigGroup] = {}
    for name, values in sections:
        if not _KEY.fullmatch(name):
            raise ValueError(
                f"config group {name[:40]!r} must be lowercase letters, digits or _",
            )
        if name in groups:
            raise ValueError(f"duplicate config group {name}")
        label = _plain(values.get("Label", ""), MAX_GROUP_LABEL, f"ConfigGroup {name}: Label")
        if not label:
            label = BUILTIN_GROUPS.get(name, "")
        if not label:
            raise ValueError(f"ConfigGroup {name}: missing Label")
        collapsed = values.get("Collapsed")
        groups[name] = ConfigGroup(
            name=name, label=label,
            help=_plain(values.get("Help", ""), MAX_HELP, f"ConfigGroup {name}: Help"),
            collapsed=(name == ADVANCED) if collapsed is None else _flag(
                collapsed, f"group {name}", "Collapsed",
            ),
        )
    return groups


def form_groups(
    fields: Sequence[ConfigField], declared: Mapping[str, ConfigGroup],
) -> tuple[ConfigGroup, ...]:
    """The sections a form shows, in order of their first field.

    Fields without a group come first under an unnamed section; ``advanced``
    always comes last.
    """
    order: list[str] = []
    for field in fields:
        if field.group not in order:
            order.append(field.group)
    order.sort(key=lambda name: (name != "", name == ADVANCED))
    return tuple(
        declared.get(name) or ConfigGroup(
            name=name, label=BUILTIN_GROUPS.get(name, ""), collapsed=name == ADVANCED,
        )
        for name in order
    )


def parse_fields(
    groups: Sequence[tuple[str, Mapping[str, str]]],
    declared: Mapping[str, ConfigGroup] | None = None,
) -> tuple[ConfigField, ...]:
    """Fields from ``(key, group values)`` pairs in manifest order.

    ``declared`` holds the ``[ConfigGroup]`` sections a ``Group`` may name
    besides the built-in ones. Raises ValueError with a reason; the manifest
    parser turns that into a ManifestError so clients ignore the whole
    manifest.
    """
    sections = dict(declared or {})
    if len(groups) > MAX_FIELDS:
        raise ValueError(f"more than {MAX_FIELDS} config fields")
    fields: list[ConfigField] = []
    seen: set[str] = set()
    for key, values in groups:
        if not _KEY.fullmatch(key):
            raise ValueError(f"config key {key[:40]!r} must be lowercase letters, digits or _")
        if key in seen:
            raise ValueError(f"duplicate config key {key}")
        seen.add(key)
        kind = values.get("Type", "")
        if kind not in TYPES:
            raise ValueError(f"Config {key}: unknown Type {kind[:20]!r}")
        label = _plain(values.get("Label", ""), MAX_LABEL, f"Config {key}: Label")
        if not label:
            raise ValueError(f"Config {key}: missing Label")
        choices: tuple[str, ...] = ()
        if kind == "choice":
            choices = tuple(dict.fromkeys(
                part.strip() for part in values.get("Choices", "").split(";") if part.strip()
            ))
            if not choices or len(choices) > 32 or not all(_CHOICE.fullmatch(c) for c in choices):
                raise ValueError(f"Config {key}: Choices must list simple words")
        minimum = maximum = None
        if kind == "int":
            try:
                minimum = int(values["Min"]) if values.get("Min") else None
                maximum = int(values["Max"]) if values.get("Max") else None
            except ValueError:
                raise ValueError(f"Config {key}: Min and Max must be integers") from None
        if kind not in ("int",) and (values.get("Min") or values.get("Max")):
            raise ValueError(f"Config {key}: Min and Max are only for int")
        if values.get("Pattern") and kind not in ("string", "url", "secret"):
            raise ValueError(f"Config {key}: Pattern is only for text fields")
        group = values.get("Group", "").strip()
        if _flag(values.get("Advanced", "false"), key, "Advanced"):
            if group and group != ADVANCED:
                raise ValueError(f"Config {key}: Advanced=true conflicts with Group={group[:20]}")
            group = ADVANCED
        if group and group not in sections and group not in BUILTIN_GROUPS:
            raise ValueError(f"Config {key}: Group {group[:20]!r} has no [ConfigGroup] section")
        field = ConfigField(
            key=key, label=label, type=kind,
            required=_flag(values.get("Required", "false"), key),
            help=_plain(values.get("Help", ""), MAX_HELP, f"Config {key}: Help"),
            choices=choices, minimum=minimum, maximum=maximum,
            placeholder=_plain(
                values.get("Placeholder", ""), MAX_PLACEHOLDER, f"Config {key}: Placeholder",
            ),
            example=_plain(values.get("Example", ""), MAX_EXAMPLE, f"Config {key}: Example"),
            help_url=_help_url(values.get("HelpUrl", ""), key),
            group=group,
            pattern=_pattern(values.get("Pattern", ""), key),
            error_text=_plain(
                values.get("ErrorText", ""), MAX_ERROR_TEXT, f"Config {key}: ErrorText",
            ),
            show_if=_show_if(values.get("ShowIf", ""), key, fields),
        )
        if values.get("Default") not in (None, ""):
            if kind == "secret":
                raise ValueError(f"Config {key}: a secret cannot have a Default")
            raw = values["Default"]
            try:
                parsed: object = (
                    _flag(raw, key) if kind == "bool" else int(raw) if kind == "int" else raw
                )
                field = replace(field, default=field.coerce(parsed))
            except (ValueError, ConfigError):
                raise ValueError(f"Config {key}: invalid Default") from None
        fields.append(field)
    return tuple(fields)


def _show_if(value: str, key: str, earlier: Sequence[ConfigField]) -> tuple[str, str] | None:
    if not value:
        return None
    target, separator, wanted = value.partition("=")
    target, wanted = target.strip(), wanted.strip()
    source = next((field for field in earlier if field.key == target), None)
    if not separator or source is None:
        raise ValueError(f"Config {key}: ShowIf must be KEY=VALUE of an earlier setting")
    if source.secret:
        raise ValueError(f"Config {key}: ShowIf cannot depend on a secret")
    if source.type == "bool" and wanted not in ("true", "false"):
        raise ValueError(f"Config {key}: ShowIf on a bool needs true or false")
    if source.type == "choice" and wanted not in source.choices:
        raise ValueError(f"Config {key}: ShowIf names a value that is not a choice")
    _plain(wanted, 64, f"Config {key}: ShowIf")
    return target, wanted


def masked(fields: Sequence[ConfigField], values: Mapping[str, object]) -> dict[str, object]:
    """What ``GetConfig()`` may reveal: every field, secrets as a mask only."""
    shown: dict[str, object] = {}
    for field in fields:
        value = values.get(field.key)
        if field.secret:
            shown[field.key] = SECRET_MASK if value else ""
            continue
        try:
            shown[field.key] = field.empty() if value is None else field.coerce(value)
        except ConfigError:
            shown[field.key] = field.empty()
    return shown


def parse_update(text: str) -> dict[str, object]:
    """A ``SetConfig()`` argument: a JSON object of at most 16 KiB."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_CONFIG_BYTES:
        raise ConfigError("", "the settings are too large")
    try:
        value = json.loads(text)
    except ValueError:
        raise ConfigError("", "the settings are not valid JSON") from None
    if not isinstance(value, dict):
        raise ConfigError("", "the settings must be a JSON object")
    return value


def validate(
    fields: Sequence[ConfigField],
    current: Mapping[str, object],
    update: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Merge ``update`` into ``current`` and check every field.

    Returns ``(values, errors)``. ``values`` holds every non-secret field and
    only the secrets that ``update`` sets anew; a secret that is empty or
    equal to :data:`SECRET_MASK` keeps its stored value. Unknown keys are
    errors, so a typo never silently does nothing.
    """
    by_key = {field.key: field for field in fields}
    errors: dict[str, str] = {
        str(key)[:32]: "is not a setting of this plugin" for key in update if key not in by_key
    }
    values: dict[str, object] = {}
    merged = {**current, **update}
    shown = {field.key for field in visible_fields(fields, merged)}
    for field in fields:
        # A field hidden by ShowIf does not apply, so it is never required.
        required = field.required and field.key in shown
        if field.secret:
            new = update.get(field.key)
            if new in (None, "", SECRET_MASK):
                if required and not current.get(field.key):
                    errors[field.key] = "is required"
                continue
        else:
            new = update.get(field.key, current.get(field.key))
            if new is None or new == "":
                if required:
                    errors[field.key] = "is required"
                values[field.key] = field.empty()
                continue
        try:
            values[field.key] = field.coerce(new)
        except ConfigError as error:
            errors[field.key] = error.message
    return values, errors
