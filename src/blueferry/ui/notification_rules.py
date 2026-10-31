"""Settings > Notifications: what clicking an iPhone app notification opens.

The GTK counterpart of the Qt client's ``NotificationOpenMapEditor.qml``.
Rules only apply with "All iPhone Notifications", so the groups are shown
only then, and only for daemons that report the rules' revision. The
backend validates and stores every rule; the form checks the same rules as
the terminal client before it offers Add. The rules are read again only
when ``notification_open_map_revision`` in the status changes.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.notification_open_map import parse_target, validate_bundle_id

_UNREAD = object()


def _valid(check: Callable[[str], object], text: str) -> bool:
    try:
        check(text)
    except ValueError:
        return False
    return True


class NotificationRules:
    def __init__(self, client, toast: Callable[[str], None]) -> None:
        self._client = client
        self._toast = toast
        self._reachable = False
        self._shown = False
        self._revision: object = _UNREAD
        self._loaded_revision: object = _UNREAD
        self._loading_revision: object = _UNREAD
        self._read_serial = 0
        self._pending = False
        self.rules: list[dict[str, str]] = []
        self._rule_rows: list[Adw.ActionRow] = []
        self.group = Adw.PreferencesGroup(
            title=_("Clicking iPhone Notifications"),
            description=_(
                "Choose a web address (http or https) or a desktop app to open when "
                "a notification from an iPhone app is clicked. Apps without a rule "
                "keep the default behaviour. Notification text is never passed to "
                "the app or address."
            ),
        )
        self.empty_row = Adw.ActionRow(title=_("No rules yet."), use_markup=False)
        self.empty_row.add_css_class("dim-label")
        self.group.add(self.empty_row)

        self.form = Adw.PreferencesGroup()
        self.bundle_entry = Adw.EntryRow(
            title=_("iPhone app, e.g. com.apple.mobilemail"), use_markup=False,
        )
        self.bundle_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("iPhone app bundle ID")],
        )
        self.target_entry = Adw.EntryRow(
            title=_("https://… or org.mozilla.Thunderbird.desktop"), use_markup=False,
        )
        self.target_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Web address or desktop app ID to open")],
        )
        self.add_button = Gtk.Button(
            label=_("Add"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"],
        )
        self.add_button.connect("clicked", lambda _button: self.add())
        self.target_entry.add_suffix(self.add_button)
        self.bundle_entry.connect("entry-activated", lambda _row: self.target_entry.grab_focus())
        self.target_entry.connect("entry-activated", lambda _row: self.add())
        for entry in (self.bundle_entry, self.target_entry):
            entry.connect("changed", lambda _row: self._render_form())
        self.form.add(self.bundle_entry)
        self.form.add(self.target_entry)
        self.groups = (self.group, self.form)
        self._render_form()

    def apply(self, status: BackendStatus, reachable: bool) -> None:
        self._reachable = reachable
        revision = status.extra.get("notification_open_map_revision", _UNREAD)
        shown = (
            reachable and status.notification_policy == "all" and revision is not _UNREAD
        )
        for group in self.groups:
            group.set_visible(shown)
        if not shown and self._shown:
            self._read_serial += 1
            self._loaded_revision = self._loading_revision = _UNREAD
        self._shown = shown
        self._revision = revision
        # Only a successful read acknowledges a revision. Failed reads retry
        # on the next status update; older replies cannot replace newer rules.
        if shown and revision != self._loaded_revision:
            self.reload()
        self._render_form()

    def reload(self) -> None:
        if not self._shown or self._pending or self._loading_revision == self._revision:
            return
        self._read_serial += 1
        serial, revision = self._read_serial, self._revision
        self._loading_revision = revision

        def loaded(rules: object) -> None:
            if serial != self._read_serial:
                return
            self._loading_revision = _UNREAD
            if isinstance(rules, list):
                self._loaded_revision = revision
                self._updated(rules)

        def failed(message: str) -> None:
            if serial != self._read_serial:
                return
            self._loading_revision = _UNREAD
            self._toast(_("Could not load the notification rules: {error}").format(error=message))

        self._client.notification_open_map_async(loaded, failed)

    def add(self) -> None:
        bundle, target = self._drafts()
        if not self._can_add(bundle, target):
            return
        self._begin_change()
        self._client.set_notification_open_target_async(
            bundle, target, self._changed, self._failed,
        )

    def remove(self, bundle: str) -> None:
        if not self._shown or self._pending:
            return
        self._begin_change()
        self._client.remove_notification_open_target_async(
            bundle, self._changed, self._failed,
        )

    def _begin_change(self) -> None:
        self._pending = True
        self._read_serial += 1
        self._loaded_revision = self._loading_revision = _UNREAD
        self._render_form()

    def _changed(self, rules: object) -> None:
        self._pending = False
        if self._shown:
            self._updated(rules)
        self._render_form()
        self.reload()

    def _drafts(self) -> tuple[str, str]:
        return self.bundle_entry.get_text().strip(), self.target_entry.get_text().strip()

    def _can_add(self, bundle: str, target: str) -> bool:
        return (
            self._shown and self._reachable and not self._pending
            and _valid(validate_bundle_id, bundle)
            and _valid(parse_target, target)
        )

    def _failed(self, message: str) -> None:
        self._pending = False
        self._render_form()
        self._toast(_("Could not change the notification rules: {error}").format(error=message))

    def _updated(self, rules: object) -> None:
        if not isinstance(rules, list):
            return
        self.rules = [rule for rule in rules if isinstance(rule, dict)]
        for row in self._rule_rows:
            self.group.remove(row)
        self._rule_rows = []
        for rule in self.rules:
            bundle = str(rule.get("bundle_id", ""))
            row = Adw.ActionRow(
                title=bundle, subtitle=str(rule.get("target", "")), use_markup=False,
            )
            remove = Gtk.Button(
                icon_name="edit-delete-symbolic", valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove"), css_classes=["flat"],
            )
            remove.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Remove rule for {bundle}").format(bundle=bundle)],
            )
            remove.connect("clicked", lambda _button, bundle=bundle: self.remove(bundle))
            row.add_suffix(remove)
            self.group.add(row)
            self._rule_rows.append(row)
        self.empty_row.set_visible(not self.rules)
        # Clear the form only once the backend has accepted this rule.
        bundle, target = self._drafts()
        if any(rule.get("bundle_id") == bundle and rule.get("target") == target
               for rule in self.rules):
            self.bundle_entry.set_text("")
            self.target_entry.set_text("")

    def _render_form(self) -> None:
        bundle, target = self._drafts()
        self.group.set_sensitive(not self._pending)
        self.form.set_sensitive(self._reachable and not self._pending)
        self.add_button.set_sensitive(self._can_add(bundle, target))
        for entry, check, text in (
            (self.bundle_entry, validate_bundle_id, bundle),
            (self.target_entry, parse_target, target),
        ):
            if text and not _valid(check, text):
                entry.add_css_class("error")
            else:
                entry.remove_css_class("error")
