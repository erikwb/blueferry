"""Adaptive iPhone page for setup, health, preferences, and maintenance."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from blueferry.bluetooth_devices import PairedDevice
from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.onboarding import OnboardingStage, derive_stage
from blueferry.setup_client import (
    BluetoothCompatibility,
    ConfigurationState,
    SetupClient,
)
from blueferry.ui.setup_runner import SetupRunner
from blueferry.ui.status_presenter import (
    connection_subtitle,
    onboarding_presentation,
)


class IPhonePage(Gtk.Box):
    def __init__(self, client, toast) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._client = client
        self._toast = toast
        self._setup = SetupClient()
        self._setup_runner = SetupRunner(GLib.idle_add)
        self._compatibility: BluetoothCompatibility | None = None
        self._configuration: ConfigurationState | None = None
        self._last_status = BackendStatus()
        self._setup_loaded = False
        self._last_onboarding_stage: OnboardingStage | None = None
        self._applying_notification_policy = False
        self._applying_storage_policy = False

        page = Adw.PreferencesPage()
        self.append(page)

        onboarding_group = Adw.PreferencesGroup(title=_("Getting Started"))
        self._onboarding_row = Adw.ActionRow(
            title=_("Checking Bluetooth Support"),
            subtitle=_("No changes are being made"),
        )
        self._onboarding_icon = Gtk.Image(icon_name="content-loading-symbolic")
        self._onboarding_row.add_suffix(self._onboarding_icon)
        onboarding_group.add(self._onboarding_row)
        page.add(onboarding_group)

        pairing_group = Adw.PreferencesGroup(
            title=_("iPhone Pairing"),
            description=_(
                "Discovery and pairing are handled here. Confirm the matching "
                "Bluetooth code when your desktop and iPhone display it."
            ),
        )
        self._device_model = Gtk.StringList()
        self._devices: list[PairedDevice] = []
        self._device_row = Adw.ComboRow(
            title=_("Bluetooth Device"), model=self._device_model,
        )
        self._device_row.connect(
            "notify::selected", lambda *_args: self._selection_changed()
        )
        self._hardware_row = Adw.ActionRow(
            title=_("Bluetooth Controller"),
            subtitle=_("Checking compatibility…"),
        )
        self._bluez_row = Adw.ActionRow(
            title=_("Bluetooth Support"),
            subtitle=_("Checking system configuration…"),
        )
        self._activate_button = Gtk.Button(
            label=_("Activate"), valign=Gtk.Align.CENTER,
        )
        self._activate_button.connect("clicked", self._confirm_activate_bluez)
        self._bluez_row.add_suffix(self._activate_button)
        pairing_group.add(self._hardware_row)
        pairing_group.add(self._bluez_row)
        pairing_group.add(self._device_row)

        actions = Adw.ActionRow(
            title=_("Discover and Configure"),
            subtitle=_("Finds the iPhone and completes Linux-side setup"),
        )
        self._setup_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        actions.add_suffix(self._setup_spinner)
        self._scan_button = Gtk.Button(label=_("Scan"), valign=Gtk.Align.CENTER)
        self._scan_button.connect("clicked", lambda _button: self._load_devices(scan=True))
        self._pair_button = Gtk.Button(
            label=_("Pair or Repair"), valign=Gtk.Align.CENTER,
            css_classes=["suggested-action"],
        )
        self._pair_button.connect("clicked", self._complete_pairing)
        actions.add_suffix(self._scan_button)
        actions.add_suffix(self._pair_button)
        pairing_group.add(actions)

        forget = Adw.ActionRow(
            title=_("Forget This Device"),
            subtitle=_("Use before a clean re-pair on both devices"),
        )
        self._forget_button = Gtk.Button(
            label=_("Forget"), valign=Gtk.Align.CENTER,
            css_classes=["destructive-action"],
        )
        self._forget_button.connect("clicked", self._confirm_forget)
        forget.add_suffix(self._forget_button)
        pairing_group.add(forget)

        daemon_group = Adw.PreferencesGroup(title=_("Connection Details"))
        recheck = Gtk.Button(label=_("Recheck"), valign=Gtk.Align.CENTER)
        recheck.connect("clicked", lambda _b: self._refresh())
        daemon_group.set_header_suffix(recheck)
        self._daemon_row = Adw.ActionRow(title=_("Background Service"))
        self._daemon_icon = Gtk.Image()
        self._daemon_row.add_suffix(self._daemon_icon)
        self._map_row = Adw.ActionRow(title=_("Messages"), subtitle=_("Checking…"))
        self._map_icon = Gtk.Image()
        self._map_row.add_suffix(self._map_icon)
        daemon_group.add(self._daemon_row)
        daemon_group.add(self._map_row)
        self._pbap_row = Adw.ActionRow(title=_("Contacts"))
        self._ancs_row = Adw.ActionRow(title=_("iPhone Notifications"))
        daemon_group.add(self._pbap_row)
        daemon_group.add(self._ancs_row)
        page.add(daemon_group)

        notification_group = Adw.PreferencesGroup(
            title=_("Desktop Notifications"),
            description=_("Choose which iPhone events create desktop popups."),
        )
        policy_model = Gtk.StringList.new([
            _("All iPhone Notifications"),
            _("Messages Only"),
            _("None"),
        ])
        self._notification_policy_row = Adw.ComboRow(
            title=_("Show Popups"),
            subtitle=_("Messages only is the default"),
            model=policy_model,
        )
        self._notification_policy_row.set_selected(1)
        self._notification_policy_row.connect(
            "notify::selected", self._notification_policy_changed
        )
        notification_group.add(self._notification_policy_row)
        page.add(notification_group)
        page.add(pairing_group)

        data_group = Adw.PreferencesGroup(title=_("Local Data"))
        history_model = Gtk.StringList.new([
            _("Encrypted with Desktop Keyring"),
            _("Do Not Retain Local Data"),
        ])
        self._storage_policy_row = Adw.ComboRow(
            title=_("Storage"),
            subtitle=_("Protects message history and cached contacts"),
            model=history_model,
        )
        self._storage_policy_row.connect(
            "notify::selected", self._storage_policy_changed
        )
        data_group.add(self._storage_policy_row)
        self._storage_row = Adw.ActionRow(title=_("Storage Security"))
        self._unlock_storage_button = Gtk.Button(
            label=_("Unlock"), valign=Gtk.Align.CENTER,
        )
        self._unlock_storage_button.connect("clicked", self._unlock_storage)
        self._storage_row.add_suffix(self._unlock_storage_button)
        data_group.add(self._storage_row)
        self._contacts_row = Adw.ActionRow(title=_("Contact Destinations"))
        self._events_row = Adw.ActionRow(title=_("History Events"))
        data_group.add(self._contacts_row)
        data_group.add(self._events_row)
        sync_row = Adw.ActionRow(
            title=_("Refresh Contacts"),
            subtitle=_("Pull the current contact list from the iPhone"),
        )
        sync_button = Gtk.Button(label=_("Sync"), valign=Gtk.Align.CENTER)
        sync_button.connect("clicked", self._sync_contacts)
        sync_row.add_suffix(sync_button)
        data_group.add(sync_row)
        clear_row = Adw.ActionRow(
            title=_("Clear Local History"),
            subtitle=_("Does not delete contacts or messages from the iPhone"),
        )
        clear_button = Gtk.Button(
            label=_("Clear"), valign=Gtk.Align.CENTER,
            css_classes=["destructive-action"],
        )
        clear_button.connect("clicked", self._confirm_clear_history)
        clear_row.add_suffix(clear_button)
        data_group.add(clear_row)
        page.add(data_group)

        checklist = Adw.PreferencesGroup(
            title=_("iPhone Settings"),
            description=_("In Settings → Bluetooth, tap ⓘ next to this computer, "
                          "then enable these options:"))
        for item, sub in (
            # ActionRow subtitles are parsed as Pango markup.
            (_("Show Message Notifications"),
             GLib.markup_escape_text(_("SMS & iMessage"))),
            (_("Sync Contacts"), _("Resolves phone numbers and Apple IDs")),
            (_("Notification Access"), _("Authorized during pairing; some iOS "
             "versions show no separate toggle")),
        ):
            checklist.add(Adw.ActionRow(title=item, subtitle=sub))
        page.add(checklist)

        client.connect("availability-changed", lambda *_: self._refresh())
        client.connect("status-invalidated", self._status_invalidated)
        self._load_setup_state()
        if SetupClient().configuration().configured:
            self._refresh()

    def _selected_device(self) -> PairedDevice | None:
        selected = self._device_row.get_selected()
        return self._devices[selected] if selected < len(self._devices) else None

    def _set_pairing_busy(self, busy: bool) -> None:
        self._setup_spinner.set_spinning(busy)
        self._activate_button.set_sensitive(not busy)
        self._scan_button.set_sensitive(not busy)
        selected = self._selected_device()
        pairing_ready = bool(
            self._compatibility and self._compatibility.pairing_ready
        )
        self._pair_button.set_sensitive(
            not busy and pairing_ready and bool(selected)
        )
        self._pair_button.set_label(
            _("Use Existing Pairing")
            if selected and selected.paired else _("Pair iPhone")
        )
        self._forget_button.set_sensitive(
            not busy and bool(selected and selected.paired)
        )

    def _selection_changed(self) -> None:
        self._set_pairing_busy(False)
        self._update_onboarding()

    def _run_setup(self, operation, on_done) -> None:
        """Run blocking BlueZ setup work away from GTK's main loop."""
        self._set_pairing_busy(True)

        self._setup_runner.run(
            operation,
            lambda value: self._setup_completed(on_done, value),
            self._setup_failed,
        )

    def _setup_failed(self, message: str) -> bool:
        self._set_pairing_busy(False)
        self._toast(_("Setup failed: {error}").format(error=message))
        return False

    def _setup_completed(self, on_done, value) -> bool:
        self._set_pairing_busy(False)
        on_done(value)
        return False

    def _load_setup_state(self, *, scan_after: bool = False) -> None:
        def operation():
            return self._setup.compatibility(), self._setup.configuration()

        def loaded(value) -> None:
            compatibility, configuration = value
            self._compatibility = compatibility
            self._configuration = configuration
            self._setup_loaded = True
            active = compatibility.bearer_api_active
            if not compatibility.hardware_supported:
                hardware = _("Unsupported")
            elif compatibility.notifications_supported:
                hardware = _("Compatible")
            else:
                hardware = _("Compatible for Messages and Contacts")
            self._hardware_row.set_subtitle(
                _("{adapter} — {status}").format(
                    adapter=compatibility.adapter or _("No Adapter"),
                    status=hardware,
                )
            )
            if not compatibility.notifications_supported:
                self._bluez_row.set_subtitle(
                    _("Not required; per-app notifications are unsupported")
                )
            else:
                self._bluez_row.set_subtitle(
                    _("Active") if active
                    else _("A one-time Bluetooth restart is required")
                )
            self._activate_button.set_visible(
                compatibility.notifications_supported and not active
            )
            self._set_pairing_busy(False)
            self._update_onboarding()
            if scan_after or not self._devices:
                self._load_devices(scan=scan_after)

        self._run_setup(operation, loaded)

    def _confirm_activate_bluez(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Restart Bluetooth?"),
            body=_(
                "This restarts Bluetooth once and briefly disconnects headphones, "
                "keyboards, and other Bluetooth devices. Polkit may ask for authentication."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("restart", _("Restart Bluetooth"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)

        def responded(_current, response: str) -> None:
            if response != "restart":
                return

            def activated(_value) -> None:
                self._toast(_("Bluetooth support activated"))
                self._load_setup_state(scan_after=True)

            self._run_setup(self._setup.activate_bluez, activated)

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _load_devices(self, *, scan: bool) -> None:
        def loaded(devices: list[PairedDevice]) -> None:
            selected_adapter = (
                self._compatibility.adapter if self._compatibility else ""
            )
            matching = [
                item for item in devices
                if not selected_adapter
                or item.adapter_path.endswith(f"/{selected_adapter}")
            ]
            likely = [item for item in matching if item.likely_iphone]
            self._devices = likely or matching
            labels = []
            for item in self._devices:
                if item.paired:
                    template = _("{name} — {mac} (paired)")
                else:
                    template = _("{name} — {mac}")
                labels.append(template.format(name=item.name, mac=item.mac))
            self._device_model.splice(
                0, self._device_model.get_n_items(), labels,
            )
            self._device_row.set_selected(0 if self._devices else Gtk.INVALID_LIST_POSITION)
            self._set_pairing_busy(False)
            self._update_onboarding()
            if scan and not self._devices:
                self._toast(
                    _("No Bluetooth devices found; unlock the iPhone and keep "
                      "Bluetooth settings open")
                )

        self._run_setup(
            lambda: self._setup.devices(scan_seconds=8 if scan else 0),
            loaded,
        )

    def _complete_pairing(self, _button) -> None:
        device = self._selected_device()
        if not device:
            self._toast(_("Scan for and select an iPhone first"))
            return
        self._toast(_(
            "Preparing secure pairing — the code can take about 15 seconds to appear"
        ))

        def completed(result) -> None:
            self._configuration = ConfigurationState(
                configured=True,
                mac=device.mac,
                adapter=device.adapter_path.rsplit("/", 1)[-1],
                path="",
            )
            if (
                self._compatibility
                and self._compatibility.notifications_supported
                and not result.ancs_ready
            ):
                message = _(
                    "Pairing is complete; iPhone notification access is still "
                    "settling. Keep Bluetooth settings open."
                )
            else:
                message = _(
                    "Linux setup is complete; finish the two iPhone settings"
                )
            self._toast(message)
            self._load_devices(scan=False)
            self._update_onboarding()
            self._refresh()

        self._run_setup(lambda: self._setup.complete(device.mac), completed)

    def _confirm_forget(self, _button) -> None:
        device = self._selected_device()
        if not device or not device.paired:
            return
        dialog = Adw.AlertDialog(
            heading=_("Forget {name}?").format(name=device.name),
            body=_(
                "For a clean re-pair, also forget this computer in the iPhone's "
                "Bluetooth settings."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("forget", _("Forget"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_current, response: str) -> None:
            if response == "forget":
                self._run_setup(
                    lambda: self._setup.forget(device.mac),
                    lambda _value: self._load_devices(scan=False),
                )

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _refresh(self) -> None:
        self._client.get_status_async(self._apply_status, self._status_failed)

    def _status_invalidated(self, _client) -> None:
        self._refresh()

    def _status_failed(self, _message: str) -> bool:
        self._apply_status(BackendStatus())
        return False

    def _apply_status(self, status: BackendStatus) -> bool:
        self._last_status = status
        values = status.to_dict()
        reachable = status.daemon
        self._client.record_status(status)
        self._daemon_row.set_subtitle(
            connection_subtitle(values, reachable=reachable)
        )
        self._daemon_icon.set_from_icon_name(
            "emblem-ok-symbolic" if reachable else "dialog-warning-symbolic")

        healthy = status.map
        self._map_row.set_subtitle(
            _("Connected") if healthy
            else _("Unavailable — Check the iPhone Settings Below"))
        self._map_icon.set_from_icon_name(
            "emblem-ok-symbolic" if healthy else "dialog-warning-symbolic")

        for row, key in (
            (self._pbap_row, "pbap"),
            (self._ancs_row, "ancs"),
        ):
            row.set_subtitle(_("Connected") if values.get(key) else _("Unavailable"))
        self._contacts_row.set_subtitle(str(status.contacts))
        self._events_row.set_subtitle(str(status.events))
        policy = status.notification_policy
        selected = {"all": 0, "messages": 1, "none": 2}.get(policy, 1)
        self._applying_notification_policy = True
        self._notification_policy_row.set_selected(selected)
        self._notification_policy_row.set_sensitive(reachable)
        self._applying_notification_policy = False
        self._applying_storage_policy = True
        self._storage_policy_row.set_selected(
            1 if status.storage_policy == "none" else 0
        )
        self._storage_policy_row.set_sensitive(reachable)
        self._applying_storage_policy = False
        self._storage_row.set_subtitle(
            status.storage_detail or _("Storage status unavailable")
        )
        locked = status.storage_policy == "encrypted" and status.storage_state != "ready"
        self._unlock_storage_button.set_label(
            _("Set Up") if "one-time" in status.storage_detail else _("Unlock")
        )
        self._unlock_storage_button.set_visible(locked)
        self._unlock_storage_button.set_sensitive(reachable and locked)
        self._update_onboarding()
        return False

    def _notification_policy_changed(self, _row, _property) -> None:
        if self._applying_notification_policy:
            return
        selected = self._notification_policy_row.get_selected()
        policy = {0: "all", 1: "messages", 2: "none"}.get(selected)
        if policy is None:
            return
        self._notification_policy_row.set_sensitive(False)

        def saved(_value: str) -> None:
            self._toast(_("Desktop notification preference saved"))
            self._refresh()

        def failed(error: str) -> None:
            self._toast(
                _("Could not save notification preference: {error}").format(
                    error=error
                )
            )
            self._apply_status(self._last_status)

        self._client.set_notification_policy_async(policy, saved, failed)

    def _storage_policy_changed(self, _row, _property) -> None:
        if self._applying_storage_policy:
            return
        policy = "none" if self._storage_policy_row.get_selected() == 1 else "encrypted"
        if policy == "none":
            dialog = Adw.AlertDialog(
                heading=_("Stop Retaining Local Data?"),
                body=_(
                    "This clears message history and cached contacts, then "
                    "removes BlueFerry's storage key. Nothing on the iPhone "
                    "is deleted."
                ),
            )
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("disable", _("Clear and Stop Retaining"))
            dialog.set_close_response("cancel")
            dialog.set_response_appearance(
                "disable", Adw.ResponseAppearance.DESTRUCTIVE
            )
            dialog.connect(
                "response",
                lambda _dialog, response: (
                    self._save_storage_policy("none")
                    if response == "disable"
                    else self._apply_status(self._last_status)
                ),
            )
            dialog.present(self.get_root())
            return
        self._save_storage_policy(policy)

    def _save_storage_policy(self, policy: str) -> None:
        self._storage_policy_row.set_sensitive(False)

        def saved(_value: dict) -> None:
            self._toast(_(
                "Local data will not be retained"
                if policy == "none" else "Encrypted local storage enabled"
            ))
            self._refresh()

        def failed(error: str) -> None:
            self._toast(_("Could not change local storage: {error}").format(error=error))
            self._apply_status(self._last_status)

        self._client.set_storage_policy_async(policy, saved, failed)

    def _unlock_storage(self, _button) -> None:
        self._unlock_storage_button.set_sensitive(False)
        self._client.unlock_storage_async(
            lambda _value: self._refresh(),
            lambda error: (
                self._toast(_("Could not unlock storage: {error}").format(error=error)),
                self._refresh(),
            ),
        )

    def _update_onboarding(self) -> None:
        compatibility = (
            self._compatibility.to_dict() if self._compatibility else {}
        )
        configured = bool(
            self._configuration and self._configuration.configured
        )
        stage = derive_stage(
            setup_loaded=self._setup_loaded,
            configured=configured,
            compatibility=compatibility,
            status=self._last_status,
        )
        title, subtitle, icon = onboarding_presentation(
            stage,
            incompatibility=self._compatibility.issue if self._compatibility else "",
        )
        self._onboarding_row.set_title(title)
        self._onboarding_row.set_subtitle(subtitle)
        self._onboarding_icon.set_from_icon_name(icon)
        if (
            stage in {OnboardingStage.READY, OnboardingStage.READY_WITHOUT_ANCS}
            and self._last_onboarding_stage not in {
                OnboardingStage.READY, OnboardingStage.READY_WITHOUT_ANCS,
            }
        ):
            self._toast(_("Setup verified — BlueFerry is ready"))
        self._last_onboarding_stage = stage

    def _sync_contacts(self, _button) -> None:
        self._client.sync_contacts(
            on_ok=lambda count: (
                self._toast(_("Synced {count} contact destinations").format(count=count)),
                self._refresh(),
            ),
            on_err=lambda error: self._toast(
                _("Contact sync failed: {error}").format(error=error)
            ),
        )

    def _confirm_clear_history(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Clear Local History?"),
            body=_(
                "This permanently deletes local message history and group "
                "metadata. It does not delete anything from the iPhone."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("clear", _("Clear"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_dialog, response: str) -> None:
            if response != "clear":
                return
            self._client.clear_history_async(
                lambda: (self._toast(_("Local history cleared")), self._refresh()),
                lambda error: self._toast(
                    _("Clear failed: {error}").format(error=error)
                ),
            )

        dialog.connect("response", responded)
        dialog.present(self.get_root())
