"""Host side of the plugin surfaces (PLUGINS.md, ApiVersion 1.2), for every client.

The Qt card, ``blueferry cards`` and ``blueferry send`` build the same
things from these helpers: the "From Plugins" card section (capability
``card``), the "Send to…" targets (capability ``share``) and the outcome of
an action. Functions marked *blocking* call plugins over D-Bus with
timeouts and belong on a worker thread or in the CLI. They never raise for a
misbehaving plugin: a crash, a timeout or broken JSON becomes a dimmed hint
on that plugin's section. Everything returned is plain text.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from blueferry import __version__
from blueferry.i18n import _
from blueferry.plugin_api import CAPABILITY_CARD, CAPABILITY_SHARE
from blueferry.plugin_api.client import PluginClient, PluginError, plain_text
from blueferry.plugin_api.manifest import Discovery, PluginManifest, discover
from blueferry.plugin_api.surfaces import Action, CardItem, valid_id
from blueferry.plugin_prefs import disabled_plugins
from blueferry.text_safety import control_text

ClientFactory = Callable[[PluginManifest], PluginClient]
# How many plugins the card asks; more are listed as one skipped line.
MAX_CARD_PLUGINS = 8


def surface_plugins(
    capability: str,
    found: Discovery | None = None,
    disabled: frozenset[str] | None = None,
) -> list[PluginManifest]:
    """Enabled plugins with ``capability``; only manifest files are read."""
    found = found if found is not None else discover(blueferry_version=__version__)
    disabled = disabled if disabled is not None else disabled_plugins()
    return [plugin for plugin in found.with_capability(capability) if plugin.id not in disabled]


def card_bus_names() -> list[str]:
    """Bus names of the enabled ``card`` plugins; their owners may say
    CardChanged. Manifest files only."""
    return [plugin.bus_name for plugin in surface_plugins(CAPABILITY_CARD)[:MAX_CARD_PLUGINS]]


def find_plugin(plugin_id: str, capability: str) -> PluginManifest | None:
    for plugin in surface_plugins(capability):
        if plugin_id in (plugin.id, plugin.alias):
            return plugin
    return None


def _reason(error: Exception) -> str:
    return plain_text(error, 200) or _("the plugin failed")


# ---- card ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PluginCard:
    """One plugin's part of the "From Plugins" section."""

    plugin_id: str
    name: str
    ok: bool
    # Shown dimmed instead of the items when ``ok`` is false.
    hint: str = ""
    items: list[CardItem] = field(default_factory=list)
    # The plugin also has ``share``: its actions with ``send_to`` (1.4) pick
    # files and call SendFiles. Without it they are ordinary buttons.
    can_send: bool = False


def load_cards(
    plugins: Sequence[PluginManifest] | None = None,
    *,
    client_factory: ClientFactory = PluginClient,
) -> list[PluginCard]:
    """*Blocking.* The card items of every enabled ``card`` plugin."""
    plugins = list(plugins) if plugins is not None else surface_plugins(CAPABILITY_CARD)
    cards = []
    for plugin in plugins[:MAX_CARD_PLUGINS]:
        try:
            items = client_factory(plugin).card_items()
        except PluginError as error:
            cards.append(PluginCard(plugin.id, plugin.name, False,
                                    _("Unavailable: {reason}").format(reason=_reason(error))))
            continue
        except Exception as error:  # never let one plugin take the card down
            cards.append(PluginCard(plugin.id, plugin.name, False,
                                    _("Unavailable: {reason}").format(
                                        reason=type(error).__name__)))
            continue
        cards.append(PluginCard(plugin.id, plugin.name, True, "", items,
                                plugin.has(CAPABILITY_SHARE)))
    skipped = plugins[MAX_CARD_PLUGINS:]
    if skipped:
        cards.append(PluginCard(
            "", _("More plugins"), False,
            _("Not shown: {plugins}. Disable some under Settings > Plugins.").format(
                plugins=", ".join(plugin.name for plugin in skipped)),
        ))
    return cards


def send_target(card: PluginCard, action: Action) -> str:
    """The share target a card action sends files to, or ``""``."""
    return (action.send_to or "") if card.can_send else ""


def drop_target(card: PluginCard, item: CardItem) -> str:
    """Where files dropped on ``item`` go (its first sending action), or ``""``."""
    return (item.send_target or "") if card.can_send else ""


def card_rows(cards: Iterable[PluginCard]) -> list[dict[str, object]]:
    """Plain dicts for QML: one entry per plugin with its items.

    ``sendTo`` on an action and ``dropTarget`` on an item are share target
    ids (ApiVersion 1.4); empty when the action only runs InvokeAction.
    Action labels go on buttons and tooltips, which detect rich text, so
    they come escaped (:func:`blueferry.text_safety.control_text`);
    ``labelPlain`` is the raw label for toolkits whose buttons never read
    markup (GTK). Titles and subtitles are for plain-text labels.
    """
    return [
        {
            "pluginId": card.plugin_id, "name": card.name, "ok": card.ok, "hint": card.hint,
            "items": [
                {
                    "id": item.id, "icon": item.icon or "preferences-plugin",
                    "title": item.title, "subtitle": item.subtitle or "",
                    "dropTarget": drop_target(card, item),
                    "actions": [
                        {"id": action.id, "label": control_text(action.label),
                         "labelPlain": action.label, "icon": action.icon or "",
                         "primary": action.kind == "primary",
                         "sendTo": send_target(card, action)}
                        for action in item.actions
                    ],
                }
                for item in card.items
            ],
        }
        for card in cards
    ]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a client shows after an action or a send: never raw plugin errors."""

    ok: bool
    message: str
    open_uri: str | None = None
    job: str | None = None


def invoke(
    plugin: PluginManifest | None,
    item_id: str,
    action_id: str,
    *,
    notify: bool = False,
    client_factory: ClientFactory = PluginClient,
) -> Outcome:
    """*Blocking.* Run one card (or popup) action; ``open_uri`` is checked."""
    if plugin is None:
        return Outcome(False, _("The plugin is no longer installed or enabled."))
    try:
        result = client_factory(plugin).invoke_action(item_id, action_id, {}, notify=notify)
    except PluginError as error:
        return Outcome(False, _("{plugin}: {reason}").format(
            plugin=plugin.name, reason=_reason(error)))
    message = result.message or ("" if result.ok else _("{plugin} could not do that.").format(
        plugin=plugin.name))
    return Outcome(result.ok, message, result.open_uri)


# ---- share --------------------------------------------------------------------


def share_available() -> bool:
    """Whether "Send to…" has a plugin behind it; reads manifests only and
    starts nothing (asking for the targets would activate every plugin)."""
    return bool(surface_plugins(CAPABILITY_SHARE))


@dataclass(frozen=True, slots=True)
class ShareChoice:
    """One "Send to…" entry: ``key`` is ``<plugin id>:<target id>``."""

    plugin_id: str
    plugin_name: str
    target_id: str
    label: str
    icon: str = ""
    plugin_alias: str = ""

    @property
    def key(self) -> str:
        return f"{self.plugin_id}:{self.target_id}"


@dataclass(frozen=True, slots=True)
class ShareTargets:
    choices: list[ShareChoice] = field(default_factory=list)
    # Plain-text notes for plugins that did not answer.
    problems: list[str] = field(default_factory=list)


def load_targets(
    plugins: Sequence[PluginManifest] | None = None,
    *,
    client_factory: ClientFactory = PluginClient,
) -> ShareTargets:
    """*Blocking.* Targets of every enabled ``share`` plugin."""
    plugins = list(plugins) if plugins is not None else surface_plugins(CAPABILITY_SHARE)
    result = ShareTargets()
    for plugin in plugins:
        try:
            targets = client_factory(plugin).share_targets()
        except Exception as error:
            reason = _reason(error) if isinstance(error, PluginError) else type(error).__name__
            result.problems.append(_("{plugin}: {reason}").format(plugin=plugin.name,
                                                                   reason=reason))
            continue
        result.choices.extend(
            ShareChoice(plugin.id, plugin.name, target.id, target.label, target.icon,
                        plugin.alias)
            for target in targets
        )
    return result


def choice_label(choice: ShareChoice, choices: Sequence[ShareChoice]) -> str:
    """The target label; with the plugin name when two plugins share a label."""
    duplicate = sum(1 for other in choices if other.label == choice.label) > 1
    return f"{choice.label} ({choice.plugin_name})" if duplicate else choice.label


def resolve_choice(choices: Sequence[ShareChoice], spec: str | None) -> ShareChoice:
    """``PLUGIN``, ``PLUGIN:TARGET`` (id or alias) or nothing for the only one.

    Raises ``LookupError`` with a message listing the choices.
    """
    if not choices:
        raise LookupError(_("No plugin offers “Send to”. Install one under "
                            "Settings > Plugins or with “blueferry plugins install”."))
    listing = ", ".join(choice.key for choice in choices)
    if not spec:
        if len(choices) == 1:
            return choices[0]
        raise LookupError(_("Choose a target with --to: {targets}").format(targets=listing))
    plugin_part, _separator, target_part = spec.partition(":")
    matches = [
        choice for choice in choices
        if plugin_part and plugin_part in (choice.plugin_id, choice.plugin_alias)
        and (not target_part or target_part == choice.target_id)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(_("Unknown target {target}. Choose one of: {targets}").format(
            target=plain_text(spec, 120), targets=listing))
    raise LookupError(_("{target} has several targets: {targets}").format(
        target=plain_text(spec, 120), targets=", ".join(choice.key for choice in matches)))


def card_choice(
    plugin_id: str, target_id: str, label: str = "", *,
    client_factory: ClientFactory = PluginClient,
) -> ShareChoice | None:
    """*Blocking.* The share target behind a card action with ``send_to``
    (1.4), or None when the plugin is gone, disabled or has no ``share``.

    Asks the plugin whether it still offers the target (a card can be
    older than the device list); LookupError with a message if not.
    """
    if not valid_id(target_id):
        return None
    plugin = find_plugin(plugin_id, CAPABILITY_SHARE)
    if plugin is None:
        return None
    try:
        offered = {target.id for target in client_factory(plugin).share_targets()}
    except PluginError as error:
        raise LookupError(_("{plugin}: {reason}").format(
            plugin=plugin.name, reason=_reason(error))) from None
    if target_id not in offered:
        raise LookupError(_("{target} is no longer there. Refresh the card and try "
                            "again.").format(target=plain_text(label, 80) or _("The target")))
    return ShareChoice(plugin.id, plugin.name, target_id,
                       plain_text(label, 80) or plugin.name, "", plugin.alias)


def send(
    choice: ShareChoice,
    paths: Sequence[str],
    *,
    plugin: PluginManifest | None = None,
    client_factory: ClientFactory = PluginClient,
) -> Outcome:
    """*Blocking.* Hand ``paths`` to one target; a long transfer continues
    in the plugin and shows up on its card."""
    plugin = plugin or find_plugin(choice.plugin_id, CAPABILITY_SHARE)
    if plugin is None:
        return Outcome(False, _("The plugin is no longer installed or enabled."))
    try:
        result = client_factory(plugin).send_files(choice.target_id, list(paths))
    except PluginError as error:
        return Outcome(False, _("{plugin}: {reason}").format(
            plugin=plugin.name, reason=_reason(error)))
    message = result.message or (
        _("Sent to {target}.").format(target=choice.label) if result.ok
        else _("{target} did not accept the files.").format(target=choice.label)
    )
    return Outcome(result.ok, message, job=result.job)
