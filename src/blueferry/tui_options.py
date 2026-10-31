"""The terminal client's Settings screen (``o``; ``OptionsScreen`` in code),
in the settings categories of the Qt and GTK clients: Phone, Notifications,
Calls & Audio, Security, Plugins and About. The Plugins tab is
:class:`~blueferry.tui_plugin_settings.PluginsPane`.

- Saved preferences the daemon reports in ``GetStatus`` are checkboxes
  (:data:`SAVED_SWITCHES`). Each one is shown only when the daemon reports
  its key, saved through its D-Bus setter on a worker, and kept on the
  user's choice until the daemon answers; a failure puts the daemon's value
  back.
- The ``local.env`` feature switches come from ``Messages1.GetFeatures`` and
  change through ``SetFeature`` (:mod:`blueferry.features_view`, shared with
  Qt and GTK). Older daemons show them disabled with the variable to set.
  A change the running service has not picked up yet shows the restart
  notice with "Restart Service".
- Notifications > Muted apps lists the iPhone apps from
  ``Messages1.ListNotificationApps`` with a checkbox each
  (:mod:`blueferry.notification_apps_view`, shared with Qt, GTK and
  Quickshell), shown only while the daemon supports muting and read again
  on ``NotificationAppsChanged``.
- The per-key actions (reconnect ``b``, click rules ``l``) have buttons in
  their category; the sound route ``a`` and automatic reset ``B`` are
  checkboxes there. Diagnostics opens :class:`~blueferry.tui_doctor.DoctorScreen`.

Background work runs through :class:`~blueferry.tui_tasks.TextualTaskRunner`;
results arrive on the UI thread. Daemon text is never parsed as markup.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Select, Static, TabbedContent, TabPane
from textual.widgets.select import NoSelection

from blueferry import __version__, features_view, notification_apps_view
from blueferry.backend_lifecycle import restart_backend
from blueferry.doctor_report import DoctorReport, run_doctor
from blueferry.i18n import _, ngettext
from blueferry.models import BackendStatus
from blueferry.phone_audio_view import phone_audio
from blueferry.proximity_lock import clamp_grace
from blueferry.reconnect_view import reconnect_view
from blueferry.text_safety import terminal_text
from blueferry.tui_doctor import DoctorScreen
from blueferry.tui_plugin_settings import PluginsPane
from blueferry.tui_tasks import TextualTaskRunner, TextualTimer

Values = Mapping[str, Any]


class OptionsClient(Protocol):
    def status(self) -> BackendStatus: ...

    def features(self) -> dict: ...

    def set_feature(self, name: str, enabled: bool) -> str: ...

    def set_notification_policy(self, policy: str) -> str: ...

    def sync_contacts(self) -> int: ...

    # Messages1.ListNotificationApps / SetNotificationAppMuted.
    def notification_apps(self) -> dict: ...

    def set_notification_app_muted(self, app_id: str, muted: bool) -> bool: ...


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    title: str


def categories() -> tuple[Category, ...]:
    """The Qt and GTK settings categories, in their order."""
    return (
        Category("phone", _("Phone")),
        Category("notifications", _("Notifications")),
        Category("calls", _("Calls & Audio")),
        Category("security", _("Security")),
        Category("plugins", _("Plugins")),
        Category("about", _("About")),
    )


def _always(_values: Values) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class SavedSwitch:
    """A saved preference from ``GetStatus``: its key (also the checkbox id
    ``opt-<key>``), label, caption and setter.

    ``shown`` decides whether the daemon offers it (older daemons lack the
    key), ``editable`` whether it can change right now, ``value`` its state
    and ``save(client, enabled, values)`` stores a new one.
    """

    key: str
    label: str
    save: Callable[[Any, bool, Values], object]
    caption: Callable[[Values], str] = lambda _values: ""
    shown: Callable[[Values], bool] = field(default=_always)
    value: Callable[[Values], bool] | None = None
    editable: Callable[[Values], bool] = field(default=_always)

    def active(self, values: Values) -> bool:
        if self.value is not None:
            return self.value(values)
        return values.get(self.key) is True


def _reported(key: str) -> Callable[[Values], bool]:
    return lambda values: isinstance(values.get(key), bool)


def _saved_opt_in(key: str) -> Callable[[Values], bool]:
    return lambda values: values.get("saved_opt_ins") is True and key in values


def _text(value: str) -> Callable[[Values], str]:
    return lambda _values: value


def _ancs_actions_caption(values: Values) -> str:
    if values.get("notification_content_shown") is False:
        return _("Unavailable while notification content is hidden")
    if values.get("notification_policy") != "all":
        return _("Applies only to All iPhone Notifications")
    return _(
        "A click runs the action on the iPhone, for example answering or "
        "declining a call"
    )


def _ancs_actions_editable(values: Values) -> bool:
    # A saved "on" can always be switched off; switching on needs All
    # iPhone Notifications with content shown.
    return values.get("ancs_actions_preference") is True or (
        values.get("notification_policy") == "all"
        and values.get("notification_content_shown") is not False
    )


def _battery_caption(values: Values) -> str:
    percent = values.get("phone_battery_warning_percent", 20)
    return _("One desktop notification per discharge, at {percent} % or less.").format(
        percent=percent,
    )


def _away_lock_caption(values: Values) -> str:
    grace = clamp_grace(values.get("proximity_lock_grace_sec"))
    locks = ngettext(
        "Locks after {seconds} second away", "Locks after {seconds} seconds away", grace,
    ).format(seconds=grace)
    return locks + "  ·  " + _(
        "A convenience, not a security feature: Bluetooth presence can be "
        "spoofed. BlueFerry never unlocks the desktop."
    )


def _missed_calls(values: Values) -> bool:
    # On unless the daemon says otherwise, as in the Qt and GTK clients.
    return values.get("missed_call_notifications") is not False


SAVED_SWITCHES: dict[str, SavedSwitch] = {
    switch.key: switch for switch in (
        SavedSwitch(
            key="profile_reset_auto",
            label=_("Reset a Stuck iPhone Connection Automatically"),
            caption=_text(_(
                "Briefly disconnects the iPhone when call audio stays stuck, at "
                "most every ten minutes."
            )),
            shown=_reported("profile_reset_auto"),
            save=lambda client, enabled, _values: client.set_auto_profile_reset(enabled),
        ),
        SavedSwitch(
            key="contact_photos",
            label=_("Show Contact Photos"),
            caption=_text(_(
                "Photos are kept with your contacts and arrive with the next contact "
                "sync. The terminal can't draw them; other clients and popups show "
                "them. Turning this off erases them."
            )),
            shown=_saved_opt_in("contact_photos"),
            save=lambda client, enabled, _values: client.set_contact_photos(enabled),
        ),
        SavedSwitch(
            key="contacts_only_notifications",
            label=_("Only Notify for Contacts"),
            caption=_text(_("Unknown senders remain available in message history")),
            editable=lambda values: values.get("notification_policy") != "none",
            save=lambda client, enabled, _values: (
                client.set_contacts_only_notifications(enabled)
            ),
        ),
        SavedSwitch(
            key="ancs_actions_preference",
            label=_("Show iPhone Action Buttons"),
            caption=_ancs_actions_caption,
            shown=_reported("ancs_actions_preference"),
            editable=_ancs_actions_editable,
            save=lambda client, enabled, _values: (
                client.set_ancs_notification_actions(enabled)
            ),
        ),
        SavedSwitch(
            key="mirror_iphone_removals",
            label=_("Sync Notifications with iPhone"),
            caption=_text(_(
                "A notification removed or dismissed on the iPhone also closes its "
                "popup here. Applies to All iPhone Notifications."
            )),
            shown=_reported("mirror_iphone_removals"),
            save=lambda client, enabled, _values: (
                client.set_mirror_notification_removals(enabled)
            ),
        ),
        SavedSwitch(
            key="calls_enabled",
            label=_("Enable Phone Calls Through This Computer"),
            caption=_text(_(
                "Experimental. While this is on, the iPhone's hands-free link stays "
                "connected to this computer, so calls can ring and be answered here "
                "and their audio plays here. Working calls also need oFono; see "
                "\"Phone calls\" in the BlueFerry documentation. Make emergency "
                "calls on the iPhone itself."
            )),
            shown=lambda values: "calls_enabled" in values,
            save=lambda client, enabled, _values: client.set_calls_enabled(enabled),
        ),
        SavedSwitch(
            key="call_history_enabled",
            label=_("Keep the iPhone's Recent Calls"),
            caption=_text(_(
                "Keeps who called and when under your local storage setting. It "
                "never places, answers, or listens to calls. Turning it off erases "
                "the retained calls."
            )),
            shown=lambda values: "call_history_enabled" in values,
            save=lambda client, enabled, values: client.set_call_history(
                enabled, _missed_calls(values),
            ),
        ),
        SavedSwitch(
            key="missed_call_notifications",
            label=_("Notify Me About Missed Calls"),
            value=_missed_calls,
            shown=lambda values: "call_history_enabled" in values,
            editable=lambda values: values.get("call_history_enabled") is True,
            save=lambda client, enabled, _values: client.set_call_history(True, enabled),
        ),
        SavedSwitch(
            key="phone_battery_warning",
            label=_("Warn When the iPhone's Battery Runs Low"),
            caption=_battery_caption,
            # BackendStatus always fills phone_battery_warning; the threshold
            # comes only from daemons that offer the warning.
            shown=lambda values: "phone_battery_warning_percent" in values,
            save=lambda client, enabled, _values: client.set_phone_battery_warning(enabled),
        ),
        SavedSwitch(
            # Not a saved preference: the checkbox follows bluetoothd's route.
            key="phone_audio",
            label=_("Play the iPhone's Sound on This Computer"),
            caption=lambda values: str(phone_audio(values)["hint"]),
            shown=lambda values: phone_audio(values)["supported"] is True,
            value=lambda values: phone_audio(values)["on_pc"] is True,
            editable=lambda values: phone_audio(values)["available"] is True,
            save=lambda client, enabled, _values: client.set_phone_audio_route(
                "pc" if enabled else "phone",
            ),
        ),
        SavedSwitch(
            key="media_control_enabled",
            label=_("Show and Control What the iPhone Is Playing"),
            caption=_text(_("Uses the Bluetooth LE link that also carries notifications.")),
            shown=lambda values: "media_control_enabled" in values,
            save=lambda client, enabled, _values: client.set_media_control(enabled),
        ),
        SavedSwitch(
            key="media_mpris_enabled",
            label=_("Also Show It in the Desktop Media Controls (MPRIS)"),
            caption=_text(_(
                "Like any desktop music player, every application in your session "
                "can then read the title, artist and album."
            )),
            shown=lambda values: "media_mpris_enabled" in values,
            # The player needs media control, as in the Qt and GTK clients.
            editable=lambda values: values.get("media_control_enabled") is True,
            save=lambda client, enabled, _values: client.set_mpris_player(enabled),
        ),
        SavedSwitch(
            key="otp_autocopy",
            label=_("Copy One-Time Codes"),
            caption=_text(_(
                "From new messages of senders not in your contacts, never group "
                "chats. Codes are not stored or logged. Turning this off clears a "
                "copied code that is still on the clipboard."
            )),
            shown=_saved_opt_in("otp_autocopy"),
            save=lambda client, enabled, _values: client.set_otp_autocopy(enabled),
        ),
        SavedSwitch(
            key="proximity_lock_enabled",
            label=_("Lock the Desktop When My iPhone Goes Away"),
            caption=_away_lock_caption,
            shown=lambda values: "proximity_lock" in values,
            # The checkbox only opts in or out; the saved grace period is kept.
            save=lambda client, enabled, values: client.set_proximity_lock(
                enabled, clamp_grace(values.get("proximity_lock_grace_sec")),
            ),
        ),
    )
}


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """A ``local.env`` switch (checkbox id ``feature-<name>``)."""

    name: str
    label: str
    description: str = ""


FEATURE_ROWS: dict[str, FeatureRow] = {
    row.name: row for row in (
        FeatureRow(
            "show_notification_content", _("Show Message Content in Popups"),
            _("Off: popups name the sender only."),
        ),
        FeatureRow("mark_read_on_dismiss", _("Mark Read When a Popup Is Dismissed")),
        FeatureRow(
            "notification_history", _("Keep Recent iPhone Notifications"),
            _(
                "Keeps other apps' notifications in memory for the clients that "
                "list them, like iPhone Notifications in the command palette."
            ),
        ),
    )
}


@dataclass(frozen=True, slots=True)
class Group:
    """One titled block of a category. Items name a saved switch
    (``opt:<key>``), a feature switch (``feature:<name>``) or one of the
    screen's own rows (``row:<name>``). A group of switches only is hidden
    while the daemon offers none of them."""

    key: str
    category: str
    title: str
    items: tuple[str, ...]


GROUPS: tuple[Group, ...] = (
    Group("connection", "phone", _("Connection Health"),
          ("row:service-state", "row:reconnect", "opt:profile_reset_auto")),
    Group("diagnostics", "phone", _("Diagnostics"), ("row:doctor",)),
    Group("contacts", "phone", _("Contacts"), ("row:sync-contacts",)),
    Group("photos", "phone", _("Contact Photos"), ("opt:contact_photos",)),
    Group("desktop-notifications", "notifications", _("Desktop Notifications"),
          ("row:policy", "opt:contacts_only_notifications", "row:click-rules")),
    Group("muted-apps", "notifications", notification_apps_view.section_label(),
          ("row:muted-apps",)),
    Group("content", "notifications", _("Content and Actions"), (
        "feature:show_notification_content", "opt:ancs_actions_preference",
        "opt:mirror_iphone_removals", "feature:mark_read_on_dismiss",
    )),
    Group("history", "notifications", _("History"), ("feature:notification_history",)),
    Group("phone-calls", "calls", _("Phone Calls"), ("opt:calls_enabled",)),
    Group("call-history", "calls", _("Call History"),
          ("opt:call_history_enabled", "opt:missed_call_notifications")),
    Group("battery", "calls", _("Battery"), ("opt:phone_battery_warning",)),
    Group("sound", "calls", _("iPhone Sound"), ("opt:phone_audio",)),
    Group("media", "calls", _("Media Control"),
          ("opt:media_control_enabled", "opt:media_mpris_enabled")),
    Group("otp", "security", _("One-Time Codes"), ("opt:otp_autocopy",)),
    Group("away-lock", "security", _("Away Lock"), ("opt:proximity_lock_enabled",)),
    Group("local-data", "security", _("Local Data"), ("row:storage",)),
    Group("about", "about", _("BlueFerry"), ("row:version", "row:service", "row:help")),
)

_POLICIES = (
    (_("All iPhone Notifications"), "all"),
    (_("Messages Only"), "messages"),
    (_("None"), "none"),
)
_STORAGE = {
    "encrypted": _("Encrypted with Desktop Keyring"),
    "plaintext": _("Unencrypted Local Data"),
    "none": _("Do Not Retain Local Data"),
}


class PolicySelect(Select[str]):
    """The popup policy. Select announces its initial value with a Changed
    message from its own Mount handler; that is not a user choice, so it is
    held back here. Every later Changed comes from the user, because the
    screen renders the daemon's value inside ``prevent(Select.Changed)``."""

    def _watch_value(self, value: str | NoSelection) -> None:
        # Textual sets is_mounted only after the Mount handlers ran.
        if self.is_mounted:
            super()._watch_value(value)
            return
        with self.prevent(Select.Changed):
            super()._watch_value(value)


class MuteCheckbox(Checkbox):
    """One app's mute switch in Notifications > Muted apps."""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.app_id = str(row["app_id"])
        # A Text label: the app name comes from the iPhone.
        super().__init__(Text(_one_line(row["name"])), value=row["muted"] is True,
                         classes="options-mute")


class RestartState:
    """Whether "Restart Service" runs. Owned by the app and shared by every
    Options screen, so closing and reopening Options during a restart cannot
    start a second one."""

    def __init__(self) -> None:
        self.running = False


def _one_line(value: object) -> str:
    return terminal_text(value).replace("\n", " ")


def _merged(values: Values, key: str, result: object) -> dict[str, Any]:
    """``values`` with a setter's answer: a status map, or the saved bool."""
    merged = dict(values)
    if isinstance(result, Mapping):
        merged.update(result)
    elif isinstance(result, bool) and key in merged:
        merged[key] = result
    return merged


class OptionsScreen(ModalScreen[None]):
    """The settings categories as tabs."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,o", "close", _("Close"), show=False),
    ]

    def __init__(
        self,
        client: OptionsClient,
        *,
        category: str = "phone",
        restart: Callable[[], None] | None = None,
        doctor: Callable[[], DoctorReport] = run_doctor,
        restart_state: RestartState | None = None,
        plugins: Callable[[], Widget] | None = PluginsPane,
    ) -> None:
        super().__init__()
        self.client = client
        # Builds the Plugins tab (tui_plugin_settings); None leaves it empty.
        self._plugins = plugins
        self._restart_state = restart_state or RestartState()
        self.category = category if category in {c.key for c in categories()} else "phone"
        self._restart = restart or restart_backend
        self._doctor = doctor
        self.values: dict[str, Any] | None = None
        # Settings being stored, with the user's choice until the answer.
        self.pending: dict[str, Any] = {}
        self.features: dict[str, Any] = features_view.initial()
        self.saving_features: set[str] = set()
        # The last failed action; a failed status read is kept apart so a
        # reload after a failed save does not hide the save's error.
        self.error = ""
        self.load_error = ""
        self._closed = False
        self._tasks = TextualTaskRunner(self, group="options")
        # Muted apps: the last ListNotificationApps answer (normalized), and
        # the changes being stored with the user's choice until the answer.
        self.apps: dict[str, Any] | None = None
        self.muting: dict[str, bool] = {}
        self._apps_loading = False
        self._apps_again = False
        # The last ListNotificationApps failed: keep the earlier answer (or
        # none) and show only the error, never an empty "locked" list.
        self._apps_failed = False
        self._apps_shown: tuple[str, ...] | None = None
        self._apps_tasks = TextualTaskRunner(self, group="options-apps")
        self._status_timer = TextualTimer(self, self.load_status)
        self._features_timer = TextualTimer(self, self.load_features)

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="options-dialog", classes="dialog"):
            yield Static(_("Settings"), classes="dialog-title")
            with Horizontal(id="options-restart"):
                yield Static("", id="options-restart-notice")
                yield Button(
                    _("Restart Service"), variant="primary", id="options-restart-button",
                )
            yield Static(_("Loading…"), id="options-note", classes="dialog-copy")
            with TabbedContent(initial=f"options-{self.category}", id="options-tabs"):
                for category in categories():
                    with TabPane(category.title, id=f"options-{category.key}"):
                        with VerticalScroll(classes="options-page"):
                            if category.key == "plugins" and self._plugins is not None:
                                yield self._plugins()
                            for group in GROUPS:
                                if group.category == category.key:
                                    yield from self._compose_group(group)
            yield Static("", id="options-error")
            with Horizontal(classes="dialog-actions"):
                yield Button(_("Close"), id="options-close")

    def _compose_group(self, group: Group) -> ComposeResult:
        with Vertical(id=f"options-group-{group.key}", classes="options-group"):
            yield Static(group.title, classes="options-section")
            for item in group.items:
                kind, _sep, name = item.partition(":")
                if kind == "opt":
                    yield Checkbox(SAVED_SWITCHES[name].label, id=f"opt-{name}")
                    yield Static("", id=f"opt-{name}-caption", classes="options-caption")
                elif kind == "feature":
                    yield Checkbox(FEATURE_ROWS[name].label, id=f"feature-{name}")
                    yield Static("", id=f"feature-{name}-caption", classes="options-caption")
                else:
                    yield from self._compose_row(name)

    def _compose_row(self, name: str) -> ComposeResult:
        if name == "service-state":
            yield Static("", id="options-service-state", classes="options-caption")
        elif name == "reconnect":
            with Horizontal(id="options-reconnect-row", classes="options-action"):
                yield Static("", id="options-reconnect-hint", classes="options-action-text")
                yield Button(_("Reconnect"), id="options-reconnect")
        elif name == "doctor":
            with Horizontal(classes="options-action"):
                yield Static(_(
                    "Checks the Bluetooth controller, the BlueZ OBEX service, the "
                    "configured iPhone and the notification link, like the command "
                    "blueferry doctor."
                ), classes="options-action-text")
                yield Button(_("Run Diagnostics"), id="options-doctor")
        elif name == "sync-contacts":
            with Horizontal(classes="options-action"):
                yield Static(
                    _("Pull the current contact list from the iPhone"),
                    id="options-contacts-count", classes="options-action-text",
                )
                yield Button(_("Sync"), id="options-sync-contacts")
        elif name == "policy":
            yield Static(_("Show Popups"), classes="options-label")
            yield PolicySelect(
                _POLICIES, allow_blank=False, value="messages", id="options-policy",
            )
        elif name == "click-rules":
            with Horizontal(classes="options-action"):
                yield Static(
                    _("Choose what a click on an app's notification opens."),
                    classes="options-action-text",
                )
                yield Button(_("Click Rules"), id="options-click-rules")
        elif name == "muted-apps":
            yield Static(Text(notification_apps_view.section_description()),
                         classes="options-caption")
            yield Static("", id="options-muted-apps-hint", classes="options-caption")
            yield Vertical(id="options-muted-apps-list")
        elif name == "storage":
            yield Static("", id="options-storage", classes="options-caption")
        elif name == "version":
            yield Static(Text(_("Version: {version}").format(version=__version__)),
                         classes="options-caption")
        elif name == "service":
            with Horizontal(classes="options-action"):
                yield Static("", id="options-service", classes="options-action-text")
                yield Button(_("Restart Service"), id="options-service-restart")
        elif name == "help":
            with Horizontal(classes="options-action"):
                yield Static(_("All keys of the terminal client"),
                             classes="options-action-text")
                yield Button(_("Keyboard Shortcuts"), id="options-help")

    @property
    def restarting(self) -> bool:
        return self._restart_state.running

    # ---- loading --------------------------------------------------------

    def on_mount(self) -> None:
        self._render_state()
        self.load_status()
        self.load_features()

    def on_unmount(self) -> None:
        self._closed = True
        self._status_timer.stop()
        self._features_timer.stop()

    def load_status(self) -> None:
        def loaded(values: object) -> None:
            if isinstance(values, dict):
                self.values = values
            self.load_error = ""

        def failed(message: str) -> None:
            self.load_error = _one_line(message)

        self._tasks.submit(
            lambda: self.client.status().to_dict(), loaded, failed, self._render_state,
        )

    def status_changed(self, status: BackendStatus) -> None:
        """The app's refreshed status (StatusChanged or the periodic poll)."""
        self.values = status.to_dict()
        self._render_state()

    def load_features(self) -> None:
        def loaded(value: object) -> None:
            self.features = features_view.loaded(self.features, value)

        def failed(message: str) -> None:
            # Older daemons have no GetFeatures; the rows fall back to hints.
            self.features = features_view.unavailable(self.features, message)

        self._tasks.submit(self.client.features, loaded, failed, self._render_state)

    # ---- rendering ------------------------------------------------------

    def _render_state(self) -> None:
        values: Values = self.values or {}
        daemon = values.get("daemon") is True
        note = self.query_one("#options-note", Static)
        note.display = self.values is None
        shown: dict[str, bool] = {}
        for key, switch in SAVED_SWITCHES.items():
            shown[f"opt:{key}"] = self._render_saved(switch, values, daemon)
        feature_view = features_view.view(self.features)
        for name, row in FEATURE_ROWS.items():
            # Always shown: an older daemon gets the local.env hint instead.
            self._render_feature(row, feature_view["switches"][name], daemon)
        for group in GROUPS:
            if all(item.partition(":")[0] == "opt" for item in group.items):
                self.query_one(f"#options-group-{group.key}").display = any(
                    shown[item] for item in group.items
                )
        self._render_rows(values, daemon)
        self._render_apps(values, daemon)
        restart = self.query_one("#options-restart")
        restart.display = feature_view["restart_pending"] is True
        self.query_one("#options-restart-notice", Static).update(
            Text(feature_view["restart_notice"])
        )
        for button_id in ("#options-restart-button", "#options-service-restart"):
            self.query_one(button_id, Button).disabled = self.restarting
        message = "\n".join(part for part in (self.load_error, self.error) if part)
        error = self.query_one("#options-error", Static)
        error.display = bool(message)
        error.update(Text(message))

    def _render_saved(self, switch: SavedSwitch, values: Values, daemon: bool) -> bool:
        checkbox = self.query_one(f"#opt-{switch.key}", Checkbox)
        caption = self.query_one(f"#opt-{switch.key}-caption", Static)
        visible = self.values is not None and switch.shown(values)
        checkbox.display = caption.display = visible
        saving = switch.key in self.pending
        active = self.pending[switch.key] if saving else switch.active(values)
        # Reflect the daemon (or the choice being saved), never a stale
        # keypress; programmatic updates must not look like a user toggle.
        with self.prevent(Checkbox.Changed):
            checkbox.value = active
        checkbox.disabled = saving or not daemon or not switch.editable(values)
        text = switch.caption(values)
        caption.update(Text(_one_line(text)))
        caption.display = visible and bool(text)
        return visible

    def _render_feature(self, row: FeatureRow, switch: dict[str, Any], daemon: bool) -> None:
        checkbox = self.query_one(f"#feature-{row.name}", Checkbox)
        caption = self.query_one(f"#feature-{row.name}-caption", Static)
        saving = row.name in self.saving_features
        if not saving:
            with self.prevent(Checkbox.Changed):
                checkbox.value = switch["active"] is True
        checkbox.disabled = saving or not daemon or switch["editable"] is not True
        text = "\n".join(part for part in (row.description, switch["note"]) if part)
        caption.update(Text(text))
        caption.display = bool(text)

    def _render_rows(self, values: Values, daemon: bool) -> None:
        state = _("Running") if daemon else _("Unavailable")
        self.query_one("#options-service-state", Static).update(
            Text(_("Background Service: {state}").format(state=state))
        )
        self.query_one("#options-service", Static).update(
            Text(_("Background Service: {state}").format(state=state))
        )
        view = reconnect_view(values)
        self.query_one("#options-reconnect-row").display = view.available
        self.query_one("#options-reconnect-hint", Static).update(Text(_one_line(view.hint)))
        self.query_one("#options-reconnect", Button).display = (
            view.offered and values.get("bluez_unresponsive") is not True
        )
        policy = str(
            self.pending.get("policy") or values.get("notification_policy") or "messages"
        )
        select = self.query_one("#options-policy", Select)
        with self.prevent(Select.Changed):
            select.value = policy if policy in {p for _l, p in _POLICIES} else "messages"
        select.disabled = not daemon or "policy" in self.pending
        self.query_one("#options-click-rules", Button).disabled = not daemon
        self.query_one("#options-sync-contacts", Button).disabled = not daemon
        storage = str(values.get("storage_policy") or "")
        storage_state = str(values.get("storage_state") or "")
        self.query_one("#options-storage", Static).update(Text(_one_line(
            _("Storage: {policy} ({state}). Change it with "
              "blueferry storage-policy-set encrypted|plaintext|none.").format(
                policy=_STORAGE.get(storage, storage or "…"), state=storage_state or "…",
            )
        )))

    # ---- muted apps -------------------------------------------------------

    def _apps_supported(self) -> bool:
        return self.values is not None and notification_apps_view.supported(self.values)

    def apps_changed(self) -> None:
        """``NotificationAppsChanged``: read the list again while it is shown."""
        if self._apps_supported():
            self.load_apps()

    def load_apps(self) -> None:
        if self._closed:
            return
        if self._apps_loading:
            self._apps_again = True
            return
        self._apps_loading = True

        def loaded(value: object) -> None:
            if not self._apps_again:
                self.apps = notification_apps_view.normalize(value)
                self._apps_failed = False

        def failed(message: str) -> None:
            if not self._apps_again:
                self._apps_failed = True
                self.error = _one_line(notification_apps_view.change_failed_text(message))

        def finished() -> None:
            self._apps_loading = False
            if self._apps_again:
                self._apps_again = False
                self.load_apps()
            self._render_state()

        self._apps_tasks.submit(self.client.notification_apps, loaded, failed, finished)

    def _render_apps(self, values: Values, daemon: bool) -> None:
        group = self.query_one("#options-group-muted-apps")
        group.display = supported = self._apps_supported()
        if not supported:
            return
        hint = self.query_one("#options-muted-apps-hint", Static)
        if self.apps is None:
            # After a failure the error says why; NotificationAppsChanged or
            # reopening the options reads the list again.
            if not self._apps_loading and not self._apps_failed:
                self.load_apps()
            hint.update(Text(_("Loading…")))
            hint.display = not self._apps_failed
            return
        text = notification_apps_view.apps_hint(self.apps)
        hint.update(Text(_one_line(text)))
        hint.display = bool(text)
        rows = notification_apps_view.app_rows(self.apps)
        ids = tuple(row["app_id"] for row in rows)
        if ids != self._apps_shown:
            self._apps_shown = ids

            async def rebuild() -> None:
                await self._rebuild_apps(rows)

            # Textual's overloads infer Never for an async function.
            self.run_worker(rebuild, group="options-apps-render", exclusive=True)  # type: ignore[arg-type]
            return
        self._apply_apps(rows, daemon)

    async def _rebuild_apps(self, rows: list[dict[str, Any]]) -> None:
        container = self.query_one("#options-muted-apps-list", Vertical)
        await container.remove_children()
        widgets: list[Static | MuteCheckbox] = []
        for row in rows:
            widgets.append(MuteCheckbox(row))
            widgets.append(Static(Text(_one_line(row["subtitle"])),
                                  classes="options-caption options-mute-caption"))
        if widgets:
            await container.mount_all(widgets)
        values = self.values or {}
        self._apply_apps(rows, values.get("daemon") is True)

    def _apply_apps(self, rows: list[dict[str, Any]], daemon: bool) -> None:
        by_id = {row["app_id"]: row for row in rows}
        for checkbox in self.query(MuteCheckbox):
            row = by_id.get(checkbox.app_id)
            if row is None:
                continue
            saving = checkbox.app_id in self.muting
            with self.prevent(Checkbox.Changed):
                checkbox.value = self.muting[checkbox.app_id] if saving else row["muted"]
            checkbox.disabled = saving or not daemon or row["enabled"] is not True

    def _set_muted(self, app_id: str, muted: bool) -> None:
        if self.apps is None or app_id in self.muting:
            return
        rows = notification_apps_view.app_rows(self.apps)
        index = next((i for i, row in enumerate(rows) if row["app_id"] == app_id), -1)
        if index < 0 or rows[index]["muted"] == muted:
            return
        target = notification_apps_view.toggle_at(self.apps, index)
        if target is None:  # disabled meanwhile: show the daemon's state again
            self._render_state()
            return
        self.muting[app_id] = muted

        def saved(_result: object) -> None:
            self.error = ""

        def failed(message: str) -> None:
            self.error = _one_line(notification_apps_view.change_failed_text(message))

        def finished() -> None:
            self.muting.pop(app_id, None)
            # NotificationAppsChanged follows too; this covers a lost signal.
            self.load_apps()
            self._render_state()

        self._render_state()
        self._apps_tasks.submit(
            lambda: self.client.set_notification_app_muted(*target), saved, failed, finished,
        )

    # ---- saved switches ---------------------------------------------------

    @on(Checkbox.Changed)
    def checkbox_changed(self, event: Checkbox.Changed) -> None:
        if isinstance(event.checkbox, MuteCheckbox):
            self._set_muted(event.checkbox.app_id, event.value)
            return
        checkbox_id = event.checkbox.id or ""
        if checkbox_id.startswith("opt-"):
            self._save_switch(checkbox_id.removeprefix("opt-"), event.value)
        elif checkbox_id.startswith("feature-"):
            self._set_feature(checkbox_id.removeprefix("feature-"), event.value)

    def _save_switch(self, key: str, enabled: bool) -> None:
        switch = SAVED_SWITCHES.get(key)
        if switch is None or self.values is None or key in self.pending:
            return
        if enabled == switch.active(self.values):
            return
        values = dict(self.values)
        self.pending[key] = enabled

        def saved(result: object) -> None:
            if self.values is not None:
                self.values = _merged(self.values, key, result)
            self.error = ""

        def failed(message: str) -> None:
            self.error = _one_line(message)

        def finished() -> None:
            self.pending.pop(key, None)
            self._render_state()
            # The answer covers one key; the status brings the rest along.
            self.load_status()

        self._render_state()
        self._tasks.submit(lambda: switch.save(self.client, enabled, values),
                           saved, failed, finished)

    @on(Select.Changed, "#options-policy")
    def policy_changed(self, event: Select.Changed) -> None:
        policy = event.value
        if self.values is None or "policy" in self.pending or not isinstance(policy, str):
            return
        if policy == self.values.get("notification_policy"):
            return
        self.pending["policy"] = policy

        def saved(result: object) -> None:
            if self.values is not None:
                self.values = {**self.values, "notification_policy": str(result or policy)}
            self.error = ""

        def failed(message: str) -> None:
            self.error = _one_line(message)

        def finished() -> None:
            self.pending.pop("policy", None)
            self._render_state()
            self.load_status()

        self._render_state()
        self._tasks.submit(
            lambda: self.client.set_notification_policy(policy),
            saved, failed, finished,
        )

    # ---- feature switches and restart ------------------------------------

    def _set_feature(self, name: str, enabled: bool) -> None:
        if name in self.saving_features:
            return
        previous = self.features
        self.features = features_view.assume(previous, name, enabled)
        self.saving_features.add(name)

        def saved(result: object) -> None:
            self.features = features_view.with_notice(
                self.features, features_view.set_notice(result),
            )
            self.error = ""

        def failed(message: str) -> None:
            self.features = previous
            self.error = _("Could not save the setting: {error}").format(
                error=_one_line(message),
            )

        def finished() -> None:
            self.saving_features.discard(name)
            self._render_state()
            self.load_features()

        self._render_state()
        self._tasks.submit(lambda: self.client.set_feature(name, enabled),
                           saved, failed, finished)

    def restart_service(self) -> None:
        if self.restarting:
            return
        state = self._restart_state
        state.running = True
        self._render_state()

        def open_screens() -> list[OptionsScreen]:
            # This screen, or one opened again while the restart ran.
            # A screen pushed but not yet mounted renders the shared state
            # in its own on_mount; its widgets do not exist yet.
            return [
                screen for screen in self.app.screen_stack
                if isinstance(screen, OptionsScreen) and screen.is_mounted
                and not screen._closed
            ]

        def restarted(_value: object) -> None:
            self.app.notify(_("BlueFerry service restarted"), markup=False)
            # Options closed meanwhile reload when opened again.
            for screen in open_screens():
                screen.service_restarted()

        def failed(message: str) -> None:
            self.app.notify(
                _("Could not restart the service: {error}").format(error=_one_line(message)),
                severity="error", markup=False,
            )

        def finished() -> None:
            state.running = False
            for screen in open_screens():
                screen._render_state()

        # On the app, not this screen: closing Options must not swallow the
        # outcome of a restart that is already running.
        TextualTaskRunner(self.app, group="options-restart").submit(
            self._restart, restarted, failed, finished,
        )

    def service_restarted(self) -> None:
        self.features = features_view.with_notice(self.features, "")
        # First the status, then the switches, once the restarted daemon is
        # likely to answer (as in the Qt and GTK clients).
        self._status_timer.start(features_view.RESTART_REFRESH_DELAY_MS)
        self._features_timer.start(features_view.RESTART_FEATURES_DELAY_MS)

    @on(Button.Pressed, "#options-restart-button")
    @on(Button.Pressed, "#options-service-restart")
    def restart_button(self) -> None:
        self.restart_service()

    # ---- actions ------------------------------------------------------------

    @on(Button.Pressed, "#options-reconnect")
    async def reconnect_button(self) -> None:
        await self.app.run_action("reconnect")

    @on(Button.Pressed, "#options-click-rules")
    async def click_rules_button(self) -> None:
        await self.app.run_action("click_rules")

    @on(Button.Pressed, "#options-help")
    async def help_button(self) -> None:
        await self.app.run_action("help")

    @on(Button.Pressed, "#options-doctor")
    def doctor_button(self) -> None:
        self.open_doctor()

    def open_doctor(self) -> None:
        self.app.push_screen(DoctorScreen(self._doctor))

    @on(Button.Pressed, "#options-sync-contacts")
    def sync_contacts_button(self, event: Button.Pressed) -> None:
        event.button.disabled = True

        def synced(count: object) -> None:
            self.app.notify(
                _("Synced {count} contact destinations").format(count=count), markup=False,
            )
            self.load_status()

        def failed(message: str) -> None:
            self.app.notify(
                _("Contact sync failed: {error}").format(error=_one_line(message)),
                severity="error", markup=False,
            )

        def finished() -> None:
            event.button.disabled = False

        self._tasks.submit(
            self.client.sync_contacts,
            synced, failed, finished,
        )

    @on(TabbedContent.TabActivated, "#options-tabs")
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.category = (event.pane.id or "").removeprefix("options-") or self.category

    @on(Button.Pressed, "#options-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
