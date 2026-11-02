"""Pure presentation rules of the GTK Settings > Plugins page (no display).

The rules follow the Qt client's ``SettingsPlugins.qml``,
``PluginStoreCard.qml`` and ``PluginConfigForm.qml`` line by line: which
buttons are offered, when Save becomes available, which section is
unfolded, which notes a field shows. Every value comes from the state map
of :class:`blueferry.plugin_settings_model.PluginSettingsModel`; plugin and
index texts pass through unchanged, for plain-text widgets only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from blueferry.i18n import _

_HTTPS = re.compile(r"^https://\S+$")
_DEFAULT_GROUP = {"name": "", "label": "", "help": "", "collapsed": False}


def is_https(url: object) -> bool:
    """What Qt's ``isHttps`` accepts: an https URL without white space."""
    return bool(_HTTPS.fullmatch(str(url or "").strip()))


# ---- installed plugins and the store ---------------------------------------


def plugin_title(row: Mapping[str, Any]) -> str:
    return f"{row.get('name', '')}  {row.get('version', '')}".strip()


def plugin_subtitle(row: Mapping[str, Any]) -> str:
    ref = row.get("ref") or ""
    return " · ".join(part for part in (
        row.get("stateText") or "",
        ", ".join(row.get("capabilities") or []),
        _("version {ref}").format(ref=ref) if ref else "",
    ) if part)


def store_subtitle(card: Mapping[str, Any]) -> str:
    lines = [card.get("description") or "", " · ".join(card.get("badges") or [])]
    if card.get("state") == "update":
        lines.append(_("{installed} → {ref}").format(
            installed=card.get("installedRef") or "", ref=card.get("ref") or ""))
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class StoreAction:
    label: str
    icon: str
    sensitive: bool


def store_action(card: Mapping[str, Any], busy: bool) -> StoreAction:
    state = card.get("state")
    # "Installed ✓" carries its own check mark.
    icon = ("software-update-available-symbolic" if state == "update"
            else "" if state == "installed" else "list-add-symbolic")
    return StoreAction(card.get("stateText") or "", icon,
                       card.get("installable") is True and not busy)


def install_texts(pending: Mapping[str, Any]) -> tuple[str, str]:
    """Heading and confirm button of the install confirmation."""
    if pending.get("kind") == "update":
        return _("Update Plugin?"), _("Update")
    return _("Install Plugin?"), _("Install")


# ---- the settings form ------------------------------------------------------


def form_groups(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(config.get("groups") or [_DEFAULT_GROUP])


def group_members(fields: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [field for field in fields if (field.get("group") or "") == (name or "")]


def group_open(group: Mapping[str, Any], expanded: Mapping[str, bool], has_error: bool) -> bool:
    """Folded sections open by themselves while they hold an error."""
    name = group.get("name") or ""
    if name in expanded:
        return expanded[name]
    return not group.get("collapsed") or has_error


def field_title(field: Mapping[str, Any]) -> str:
    return f"{field.get('label', '')}{' *' if field.get('required') else ''}"


def field_notes(field: Mapping[str, Any]) -> list[str]:
    """The grey lines under a field: placeholder, help and example.

    A placeholder is always an example, never a value: "e.g. …", so it is
    not mistaken for the current setting. A stored secret is never shown.
    """
    notes = []
    if field.get("type") == "secret" and field.get("stored"):
        notes.append(_("Stored — leave empty to keep"))
    elif field.get("placeholder"):
        notes.append(_("e.g. {example}").format(example=field["placeholder"]))
    if field.get("help"):
        notes.append(str(field["help"]))
    if field.get("example"):
        notes.append(_("Example: {example}").format(example=field["example"]))
    return notes


def help_link_label(field: Mapping[str, Any]) -> str:
    """The plugin may name the link (HelpUrlLabel, ApiVersion 1.6)."""
    return str(field.get("helpUrlLabelPlain") or "") or _("More information")


def error_of(
    key: str, check: Mapping[str, Any], server_errors: Mapping[str, str], edited: set[str],
) -> str:
    """The client's pre-check wins; the plugin's reason until the user edits."""
    return str((check.get("errors") or {}).get(key) or (
        "" if key in edited else server_errors.get(key, "")))


@dataclass(frozen=True, slots=True)
class FormControls:
    ready: bool
    save: bool
    test_visible: bool
    test: bool
    sign_in_visible: bool
    sign_in: bool
    cancel_sign_in: bool
    custom: bool
    unsaved_note: bool
    fill_note: bool


def form_controls(
    config: Mapping[str, Any], check: Mapping[str, Any], draft: Mapping[str, object],
    busy: bool,
) -> FormControls:
    actions = config.get("actions") or {}
    status = config.get("status") or {}
    ready = config.get("loaded") is True and not busy
    valid = check.get("valid") is True
    unsaved = bool(draft)
    pending = status.get("pending") is True
    signing_in = status.get("kind") == "login" and pending
    return FormControls(
        ready=ready,
        save=ready and valid and unsaved,
        test_visible=actions.get("test") is True,
        test=ready and valid and not pending,
        sign_in_visible=bool(actions.get("loginLabel")) and not signing_in,
        sign_in=ready,
        cancel_sign_in=signing_in,
        # The plugin's own buttons use the stored settings: not while the
        # form has unsaved edits, and one at a time.
        custom=ready and not unsaved and not pending,
        unsaved_note=unsaved and bool(actions.get("custom")),
        fill_note=ready and not valid and not (check.get("errors") or {}),
    )


@dataclass(frozen=True, slots=True)
class StatusLine:
    visible: bool
    text: str
    style: str  # "" while pending, "success" or "error"
    spinning: bool


def status_line(status: Mapping[str, Any], *, action: bool) -> StatusLine:
    """One status line; an action's answer shows under the plugin's buttons."""
    text = str(status.get("text") or "")
    is_action = status.get("kind") == "action"
    visible = bool(text) and is_action == action
    pending = status.get("pending") is True
    style = "" if pending else ("success" if status.get("ok") is True else "error")
    return StatusLine(visible, text if visible else "", style, visible and pending)
