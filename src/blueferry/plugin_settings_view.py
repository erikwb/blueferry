"""Toolkit-free rows and forms for the plugin settings.

The Qt settings page builds its lists from these helpers: installed plugins
with status, store cards from the plugin indexes, the confirmation rows of
an install, and settings forms from a manifest's ``[Config …]`` schema.
Functions marked *blocking* talk to plugins, git or the network and belong
on a worker thread. Everything returned is plain text.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from blueferry import __version__
from blueferry.i18n import _
from blueferry.plugin_api import LOGIN_NEXTCLOUD, LOGIN_PROVIDERS, SUPPORTED_API_VERSIONS
from blueferry.plugin_api.client import PluginClient, PluginError, plain_text
from blueferry.plugin_api.config import (
    BUILTIN_GROUPS,
    SECRET_MASK,
    ConfigError,
    visible_fields,
)
from blueferry.plugin_api.manifest import PluginManifest
from blueferry.plugin_index import Catalog, PluginIndex, StoreItem, index_urls
from blueferry.plugin_manager import PluginEntry, PluginManager, PreparedInstall
from blueferry.text_safety import control_text

CAPABILITY_LABELS = {
    "photos": _("Photos"),
    "calendar": _("Calendar"),
    "conversations": _("Conversations"),
    "files": _("Files"),
    "shortcuts": _("Shortcuts"),
    "share": _("Sharing"),
}
STATE_TEXT = {
    "ok": _("Ready"),
    "unconfigured": _("Needs setup"),
    "error": _("Error"),
    "busy": _("Busy"),
}
_TRUE = {"1", "true", "yes", "on"}
LOGIN_LABELS = {LOGIN_NEXTCLOUD: _("Sign in with Nextcloud")}
GROUP_LABELS = {"account": _("Account"), "options": _("Options"), "advanced": _("Advanced")}
LOGIN_STATE_TEXT = {
    "pending": _("Waiting for the sign-in in your browser…"),
    "expired": _("The sign-in expired. Start it again."),
    "cancelled": _("The sign-in was cancelled."),
    "error": _("The sign-in failed."),
    "done": _("Signed in."),
}


def capability_label(capability: str) -> str:
    return CAPABILITY_LABELS.get(capability, capability.replace("-", " ").capitalize())


def plugin_statuses(
    entries: Sequence[PluginEntry],
    client_factory: Callable[[PluginManifest], PluginClient] = PluginClient,
) -> dict[str, tuple[str, str]]:
    """*Blocking.* ``id -> (state, detail)`` for every enabled plugin."""
    result: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not entry.enabled:
            result[entry.manifest.id] = ("disabled", "")
            continue
        try:
            status = client_factory(entry.manifest).status()
            result[entry.manifest.id] = (status.state, status.detail)
        except PluginError as error:
            result[entry.manifest.id] = ("error", plain_text(error, 200))
    return result


def entry_row(entry: PluginEntry, status: tuple[str, str] | None = None) -> dict[str, Any]:
    plugin = entry.manifest
    state, detail = status or ("unknown", "")
    if not entry.enabled:
        text = _("Disabled")
    else:
        text = STATE_TEXT.get(state, _("Checking…") if state == "unknown" else state)
    return {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "alias": plugin.alias,
        "capabilities": [capability_label(cap) for cap in plugin.capabilities],
        "source": entry.source,
        # The link button detects rich text; "source" stays plain.
        "sourceText": control_text(entry.source),
        "ref": entry.record.ref_label if entry.record else "",
        "managed": entry.managed,
        "enabled": entry.enabled,
        "state": "disabled" if not entry.enabled else state,
        "stateText": text,
        "detail": detail,
        "hasConfig": bool(plugin.config),
    }


def store_row(item: StoreItem) -> dict[str, Any]:
    entry = item.entry
    if item.update_available:
        state, text = "update", _("Update available")
    elif item.installed:
        state, text = "installed", _("Installed ✓")
    elif not entry.available:
        state, text = "soon", _("Coming soon")
    elif not item.compatible:
        state, text = "incompatible", _("Needs a newer BlueFerry")
    else:
        state, text = "install", _("Install")
    return {
        "id": entry.id,
        "name": entry.name,
        "description": entry.description,
        "icon": entry.icon or "application-x-addon",
        "emoji": entry.emoji,
        "badges": [capability_label(cap) for cap in entry.capabilities],
        "repo": entry.repo,
        "ref": entry.ref,
        "installedRef": item.installed_ref,
        "screenshot": entry.screenshot,
        "state": state,
        "stateText": text,
        "installable": state in ("install", "update"),
    }


def summary_rows(prepared: PreparedInstall) -> list[dict[str, str]]:
    labels = {
        "Plugin": _("Plugin"), "Source": _("Source"), "Ref": _("Version"),
        "Capabilities": _("Capabilities"), "Runs": _("Runs"), "Settings": _("Settings"),
        "Replaces": _("Replaces"),
    }
    rows = [{"label": labels.get(label, label), "value": value}
            for label, value in prepared.summary()]
    if prepared.changes:
        rows.append({"label": _("Changes"), "value": "\n".join(prepared.changes)})
    return rows


INT32_MIN, INT32_MAX = -2_147_483_648, 2_147_483_647


def _spin_bound(bound: int | None, default: int) -> int:
    return default if bound is None else min(max(bound, INT32_MIN), INT32_MAX)


def form_fields(manifest: PluginManifest, values: Mapping[str, object]) -> list[dict[str, Any]]:
    """One row per ``[Config …]`` field, with the current (masked) value."""
    rows = []
    for field in manifest.config:
        value = values.get(field.key, field.empty())
        show_key, show_value = field.show_if or ("", "")
        rows.append({
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "required": field.required,
            "help": field.help,
            "choices": list(field.choices),
            # A SpinBox holds 32-bit ints only; wider bounds would wrap around.
            "minimum": _spin_bound(field.minimum, INT32_MIN),
            "maximum": _spin_bound(field.maximum, INT32_MAX),
            # A stored secret is never shown, only whether one exists.
            "value": "" if field.secret else value,
            "stored": field.secret and value == SECRET_MASK,
            # ApiVersion 1.3 presentation keys; empty for older manifests.
            "placeholder": field.placeholder,
            "example": field.example,
            # https only; the manifest parser refuses anything else.
            "helpUrl": field.help_url,
            "helpUrlText": control_text(field.help_url),
            "group": field.group,
            "showIfKey": show_key,
            "showIfValue": show_value,
        })
    return rows


def form_groups(manifest: PluginManifest) -> list[dict[str, Any]]:
    """The form's sections in order; the unnamed one has an empty label."""
    return [{
        "name": group.name,
        # Built-in names get the client's translation unless relabelled.
        "label": (GROUP_LABELS.get(group.name, group.label)
                  if group.label == BUILTIN_GROUPS.get(group.name) else group.label),
        "help": group.help,
        "collapsed": group.collapsed,
    } for group in manifest.config_groups]


def form_actions(manifest: PluginManifest) -> dict[str, Any]:
    """Which helper buttons the form shows: "Test connection", "Sign in"."""
    provider = manifest.config_login if manifest.config_login in LOGIN_PROVIDERS else ""
    return {
        "test": manifest.config_test,
        "login": provider,
        "loginLabel": LOGIN_LABELS.get(provider, ""),
    }


def merged_form(
    manifest: PluginManifest, stored: Mapping[str, object], raw: Mapping[str, object],
) -> dict[str, object]:
    """The values the form shows: stored ones with the user's edits on top."""
    merged = {field.key: stored.get(field.key, field.empty()) for field in manifest.config}
    merged.update({key: value for key, value in raw.items() if key in merged})
    return merged


def check_form(
    manifest: PluginManifest, stored: Mapping[str, object], raw: Mapping[str, object],
) -> dict[str, Any]:
    """The client's pre-check of a form; the plugin's answer still decides.

    ``stored`` is what GetConfig returned (a stored secret as the mask),
    ``raw`` what the user typed. Returns ``{"errors": {key: reason},
    "visible": [keys], "valid": bool}``. A required field that is still
    empty makes the form invalid without an error text, so an untouched
    form shows no red; a field the user typed into gets one.
    """
    values, errors = parse_form(manifest, raw)
    merged = merged_form(manifest, stored, values)
    visible = visible_fields(manifest.config, merged)
    missing = False
    for field in visible:
        if field.key in errors:
            continue
        value = merged.get(field.key)
        if field.secret:
            typed = values.get(field.key)
            if field.required and not typed and stored.get(field.key) != SECRET_MASK:
                missing = True
                if field.key in raw:
                    errors[field.key] = _("is required")
            if not typed:
                continue
            value = typed
        if value is None or value == "":
            if field.required:
                missing = True
                if field.key in raw:
                    errors[field.key] = _("is required")
            continue
        try:
            field.coerce(value)
        except ConfigError as error:
            errors[field.key] = error.message
    keys = {field.key for field in visible}
    errors = {key: reason for key, reason in errors.items() if key in keys}
    return {
        "errors": errors,
        "visible": [field.key for field in visible],
        "valid": not errors and not missing,
    }


def login_values(manifest: PluginManifest, raw: Mapping[str, object]) -> dict[str, object]:
    """What a sign-in may see of the form: typed non-secret values only."""
    values, _errors = parse_form(manifest, raw)
    secrets = {field.key for field in manifest.config if field.secret}
    return {key: value for key, value in values.items() if key not in secrets}


def help_link(manifest: PluginManifest, key: str) -> str:
    """The "Where do I find this?" link of one field, or ""."""
    field = next((field for field in manifest.config if field.key == key), None)
    url = field.help_url if field is not None else ""
    return url if url.startswith("https://") else ""


def login_text(state: str, message: str = "") -> str:
    """One status line for the sign-in; the plugin's message wins."""
    return plain_text(message, 200) or LOGIN_STATE_TEXT.get(state, "")


def parse_form(
    manifest: PluginManifest, raw: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Typed values from form input; empty secrets are left out (kept)."""
    values: dict[str, object] = {}
    errors: dict[str, str] = {}
    for field in manifest.config:
        if field.key not in raw:
            continue
        value = raw[field.key]
        if field.secret:
            if isinstance(value, str) and value.strip() and value != SECRET_MASK:
                values[field.key] = value.strip()
            continue
        if field.type == "bool":
            values[field.key] = (
                value if isinstance(value, bool) else str(value).strip().casefold() in _TRUE
            )
        elif field.type == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                values[field.key] = value
            else:
                try:
                    values[field.key] = int(str(value).strip())
                except ValueError:
                    errors[field.key] = _("must be a whole number")
        else:
            values[field.key] = "" if value is None else str(value)
    return values, errors


def load_catalog(
    manager: PluginManager,
    index: PluginIndex,
    entries: Sequence[PluginEntry],
    *,
    refresh: bool = False,
) -> Catalog:
    """*Blocking.* The store cards for the configured indexes."""
    return index.catalog(
        index_urls(manager.settings()),
        installed={entry.manifest.id: entry.manifest.version for entry in entries},
        records=manager.records(),
        blueferry_version=__version__,
        supported_api=SUPPORTED_API_VERSIONS,
        refresh=refresh,
    )
