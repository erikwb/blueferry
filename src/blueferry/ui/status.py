"""Adaptive iPhone page for setup, health, preferences, and maintenance."""

from __future__ import annotations

import threading

from gi.repository import Adw, Gio, GLib, Gtk

from blueferry.bluetooth_devices import PairedDevice, iphone_candidates
from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.onboarding import OnboardingStage, derive_stage
from blueferry.quirks_report import issue_report, issue_url
from blueferry.setup_client import (
    DISCOVERY_SECONDS,
    BluetoothCompatibility,
    ConfigurationState,
    SetupClient,
)
from blueferry.setup_verification import (
    CONTACTS,
    MESSAGE_NOTIFICATIONS,
    NOTIFICATION_ACCESS,
    remaining_iphone_setup_tasks,
)
from blueferry.ui.setup_runner import SetupRunner
from blueferry.ui.status_presenter import (
    connection_subtitle,
    map_connection_refused,
    map_connection_refused_message,
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
        self._storage_unlock_attempted = False
        self._pairing_issue_report = ""
        self._compatibility_override = False
        self._applying_pairing_mode = False

        page = Adw.PreferencesPage()
        self.append(page)

        self._pairing_group = Adw.PreferencesGroup(
            title=_("Pair an iPhone"),
            description=_(
                "Scan for and select your iPhone here, then choose Pair. When the "
                "pairing request appears on the iPhone, approve it and confirm that "
                "the codes match. Pairing may appear idle for up to 15 seconds. "
                "After it completes, return to the Bluetooth device list and open "
                "this computer's ⓘ page a "
                "few times; turn on any new toggles that appear. System Notification "
                "access is also how BlueFerry recognizes group text threads; "
                "without it, a group text appears as a one-to-one conversation "
                "with its sender."
            ),
        )
        self._device_model = Gtk.StringList()
        self._devices: list[PairedDevice] = []
        self._device_row = Adw.ComboRow(
            title=_("Found iPhone"),
            subtitle=_("Choose the phone to pair"),
            model=self._device_model,
            use_markup=False,
        )
        self._device_row.connect("notify::selected", lambda *_args: self._selection_changed())
        self._hardware_row = Adw.ActionRow(
            title=_("Bluetooth Controller"),
            subtitle=_("Checking compatibility…"),
            use_markup=False,
        )
        self._adapter_model = Gtk.StringList()
        self._adapter_row = Adw.ComboRow(
            title=_("Bluetooth Controller"),
            subtitle=_("Choose which radio to pair with"),
            model=self._adapter_model,
            use_markup=False,
        )
        self._adapter_row.set_visible(False)
        self._adapter_row.connect("notify::selected", self._adapter_changed)
        self._applying_adapter = False
        self._bluez_row = Adw.ActionRow(
            title=_("Bluetooth Support"),
            subtitle=_("Checking system configuration…"),
        )
        self._activate_button = Gtk.Button(
            label=_("Activate"),
            valign=Gtk.Align.CENTER,
        )
        self._activate_button.connect("clicked", self._confirm_activate_bluez)
        self._bluez_row.add_suffix(self._activate_button)
        self._pairing_group.add(self._hardware_row)
        self._pairing_group.add(self._adapter_row)
        self._pairing_group.add(self._bluez_row)

        scan = Adw.ActionRow(
            title=_("1. Find Your iPhone"),
            subtitle=_("Scanning finds nearby devices; it does not pair them"),
        )
        self._scan_button = Gtk.Button(
            label=_("Scan for iPhone"),
            valign=Gtk.Align.CENTER,
        )
        self._scan_button.connect("clicked", lambda _button: self._load_devices(scan=True))
        scan.add_suffix(self._scan_button)
        self._pairing_group.add(scan)
        self._pairing_group.add(self._device_row)

        self._compatibility_row = Adw.ActionRow(
            title=_("Compatibility pairing"),
            subtitle=_(
                "For iOS 18 or earlier: set up Messages and Contacts without "
                "connecting ANCS"
            ),
        )
        self._compatibility_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._compatibility_switch.connect(
            "notify::active", self._pairing_mode_changed
        )
        self._compatibility_row.add_suffix(self._compatibility_switch)
        self._compatibility_row.set_activatable_widget(self._compatibility_switch)
        self._pairing_group.add(self._compatibility_row)

        self._explicit_pairing_row = Adw.ActionRow(
            title=_("Use explicit Bluetooth pairing"),
            subtitle=_(
                "Skip the initial connection attempt for controllers that "
                "cancel normal pairing"
            ),
        )
        self._explicit_pairing_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._explicit_pairing_row.add_suffix(self._explicit_pairing_switch)
        self._explicit_pairing_row.set_activatable_widget(
            self._explicit_pairing_switch
        )
        self._pairing_group.add(self._explicit_pairing_row)

        pair = Adw.ActionRow(
            title=_("2. Pair the Selected iPhone"),
            subtitle=_("Confirm the code, then watch the iPhone for new toggles"),
        )
        self._setup_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        pair.add_suffix(self._setup_spinner)
        self._pair_button = Gtk.Button(
            label=_("Pair Selected iPhone"),
            valign=Gtk.Align.CENTER,
            css_classes=["suggested-action"],
        )
        self._pair_button.connect("clicked", self._complete_pairing)
        pair.add_suffix(self._pair_button)
        self._pairing_group.add(pair)

        forget = Adw.ActionRow(
            title=_("Forget This Device"),
            subtitle=_("Use before a clean re-pair on both devices"),
        )
        self._forget_button = Gtk.Button(
            label=_("Forget"),
            valign=Gtk.Align.CENTER,
            css_classes=["destructive-action"],
        )
        self._forget_button.connect("clicked", self._confirm_forget)
        forget.add_suffix(self._forget_button)
        self._pairing_group.add(forget)
        page.add(self._pairing_group)

        self._issue_group = Adw.PreferencesGroup()
        self._issue_row = Adw.ActionRow(
            title=_("Pairing Report"),
            subtitle=_("Attach the last pairing report if notifications stay unavailable"),
            use_markup=False,
        )
        self._issue_button = Gtk.Button(
            label=_("Report Pairing Issue"),
            valign=Gtk.Align.CENTER,
        )
        self._issue_button.connect("clicked", self._file_pairing_issue)
        self._issue_row.add_suffix(self._issue_button)
        self._issue_group.add(self._issue_row)
        self._issue_group.set_visible(False)
        page.add(self._issue_group)

        self._paired_group = Adw.PreferencesGroup(title=_("Paired Phone"))
        self._paired_row = Adw.ActionRow(
            title=_("Paired iPhone"),
            subtitle=_("Checking device name…"),
            use_markup=False,
        )
        self._unpair_button = Gtk.Button(
            label=_("Unpair"),
            valign=Gtk.Align.CENTER,
            css_classes=["destructive-action"],
        )
        self._unpair_button.connect("clicked", self._confirm_forget)
        self._paired_row.add_suffix(self._unpair_button)
        self._paired_group.add(self._paired_row)
        self._paired_group.set_visible(False)
        page.add(self._paired_group)

        self._iphone_setup_group = Adw.PreferencesGroup(
            title=_("Finish Setup on the iPhone"),
            description=_(
                "Linux pairing is only the first half. Even when BlueFerry "
                "shows Connected, open Settings → Bluetooth, tap ⓘ next to "
                "this computer, and check these options. After approving “Allow "
                "System Notifications,” you may need to go back to the Bluetooth "
                "device list and reopen this computer before the other settings "
                "appear:"
            ),
        )
        self._iphone_setup_rows = {}
        for key, item, sub in (
            # ActionRow subtitles are parsed as Pango markup.
            (
                MESSAGE_NOTIFICATIONS,
                _("Show Message Notifications"),
                GLib.markup_escape_text(_("Required for SMS & iMessage")),
            ),
            (
                CONTACTS,
                _("Sync Contacts"),
                _("Required for contact names and Apple IDs"),
            ),
            (
                NOTIFICATION_ACCESS,
                _("Notification Access"),
                _(
                    "Required for system notifications and group text identification; "
                    "without it, groups look like individual conversations"
                ),
            ),
        ):
            row = Adw.ActionRow(title=item, subtitle=sub)
            self._iphone_setup_rows[key] = row
            self._iphone_setup_group.add(row)
        self._iphone_setup_group.set_visible(False)
        page.add(self._iphone_setup_group)

        daemon_group = Adw.PreferencesGroup(title=_("Connection Details"))
        recheck = Gtk.Button(label=_("Recheck"), valign=Gtk.Align.CENTER)
        recheck.connect(
            "clicked",
            lambda _b: (self._load_setup_state(), self._refresh()),
        )
        daemon_group.set_header_suffix(recheck)
        self._daemon_row = Adw.ActionRow(
            title=_("Background Service"), use_markup=False
        )
        self._daemon_icon = Gtk.Image()
        self._daemon_row.add_suffix(self._daemon_icon)
        self._map_row = Adw.ActionRow(
            title=_("Messages"), subtitle=_("Checking…"), use_markup=False
        )
        self._map_icon = Gtk.Image()
        self._map_row.add_suffix(self._map_icon)
        daemon_group.add(self._daemon_row)
        daemon_group.add(self._map_row)
        self._pbap_row = Adw.ActionRow(title=_("Contacts"), use_markup=False)
        self._pbap_icon = Gtk.Image()
        self._pbap_row.add_suffix(self._pbap_icon)
        self._ancs_row = Adw.ActionRow(
            title=_("iPhone Notifications"), use_markup=False
        )
        self._ancs_icon = Gtk.Image()
        self._ancs_row.add_suffix(self._ancs_icon)
        self._ancs_recovery_label = Gtk.Label(
            label=_(
                "FYI: If ANCS remains unavailable, BlueZ may be retaining stale "
                "Bluetooth state. Before re-pairing, run sudo systemctl restart "
                "bluetooth.service, then forget this computer on the iPhone and "
                "pair again. This briefly disconnects all Bluetooth devices."
            ),
            selectable=True,
            wrap=True,
            xalign=0,
        )
        self._ancs_recovery_label.set_margin_start(12)
        self._ancs_recovery_label.set_margin_end(12)
        self._ancs_recovery_label.set_margin_top(6)
        self._ancs_recovery_label.set_margin_bottom(6)
        self._ancs_recovery_label.set_visible(False)
        daemon_group.add(self._pbap_row)
        daemon_group.add(self._ancs_row)
        daemon_group.add(self._ancs_recovery_label)
        page.add(daemon_group)

        notification_group = Adw.PreferencesGroup(
            title=_("Desktop Notifications"),
            description=_("Choose which iPhone events create desktop popups."),
        )
        policy_model = Gtk.StringList.new(
            [
                _("All iPhone Notifications"),
                _("Messages Only"),
                _("None"),
            ]
        )
        self._notification_policy_row = Adw.ComboRow(
            title=_("Show Popups"),
            subtitle=_("Messages only is the default"),
            model=policy_model,
        )
        self._notification_policy_row.set_selected(1)
        self._notification_policy_row.connect("notify::selected", self._notification_policy_changed)
        notification_group.add(self._notification_policy_row)
        page.add(notification_group)

        data_group = Adw.PreferencesGroup(title=_("Local Data"))
        history_model = Gtk.StringList.new(
            [
                _("Encrypted with Desktop Keyring"),
                _("Unencrypted Local Data"),
                _("Do Not Retain Local Data"),
            ]
        )
        self._storage_policy_row = Adw.ComboRow(
            title=_("Storage"),
            subtitle=_("Protects message history and cached contacts"),
            model=history_model,
        )
        self._storage_policy_row.connect("notify::selected", self._storage_policy_changed)
        data_group.add(self._storage_policy_row)
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
            label=_("Clear"),
            valign=Gtk.Align.CENTER,
            css_classes=["destructive-action"],
        )
        clear_button.connect("clicked", self._confirm_clear_history)
        clear_row.add_suffix(clear_button)
        data_group.add(clear_row)
        page.add(data_group)

        client.connect("availability-changed", lambda *_: self._refresh())
        client.connect("status-invalidated", self._status_invalidated)
        self._load_setup_state()
        if self._setup.configuration().configured:
            self._refresh()

    def _selected_device(self) -> PairedDevice | None:
        selected = self._device_row.get_selected()
        return self._devices[selected] if selected < len(self._devices) else None

    def _configured_device(self) -> PairedDevice | None:
        mac = self._configuration.mac if self._configuration else ""
        return next(
            (device for device in self._devices if device.mac.casefold() == mac.casefold()),
            None,
        )

    def _update_phone_controls(self) -> None:
        configured = bool(self._configuration and self._configuration.configured)
        self._pairing_group.set_visible(not configured)
        self._paired_group.set_visible(configured)
        device = self._configured_device()
        self._paired_row.set_title(device.name if device else _("Paired iPhone"))
        self._paired_row.set_subtitle(self._configuration.mac if self._configuration else "")
        self._unpair_button.set_sensitive(configured and not self._setup_spinner.get_spinning())
        self._update_iphone_setup_tasks()

    def _update_iphone_setup_tasks(self) -> None:
        configured = bool(self._configuration and self._configuration.configured)
        notifications_supported = bool(
            self._compatibility and self._compatibility.notifications_supported
        )
        if self._configuration and self._configuration.saved:
            notifications_supported = (
                notifications_supported and self._configuration.ancs_enabled
            )
        remaining = set(
            remaining_iphone_setup_tasks(
                self._last_status.verified_iphone_setup,
                notifications_supported=notifications_supported,
            )
        )
        for key, row in self._iphone_setup_rows.items():
            row.set_visible(key in remaining)
        self._iphone_setup_group.set_visible(configured and bool(remaining))

    def _set_pairing_busy(self, busy: bool) -> None:
        self._setup_spinner.set_spinning(busy)
        self._activate_button.set_sensitive(not busy)
        self._scan_button.set_sensitive(not busy)
        self._adapter_row.set_sensitive(not busy)
        automatic_compatibility = bool(
            self._compatibility
            and not self._compatibility.notifications_supported
        )
        self._compatibility_switch.set_sensitive(
            not busy and not automatic_compatibility
        )
        self._explicit_pairing_switch.set_sensitive(not busy)
        selected = self._selected_device()
        self._pair_button.set_sensitive(not busy and bool(selected))
        self._pair_button.set_label(
            _("Use Existing Pairing") if selected and selected.paired else _("Pair Selected iPhone")
        )
        self._forget_button.set_sensitive(not busy and bool(selected and selected.paired))
        self._update_phone_controls()

    def _selection_changed(self) -> None:
        self._set_pairing_busy(False)
        self._update_onboarding()

    def _pairing_mode_changed(self, *_args) -> None:
        if self._applying_pairing_mode:
            return
        self._compatibility_override = self._compatibility_switch.get_active()
        self._set_pairing_busy(False)
        if self._compatibility is not None:
            self._apply_bluetooth_support_status(self._compatibility)
        self._update_onboarding()

    def _apply_bluetooth_support_status(self, compatibility) -> None:
        active = compatibility.bearer_api_active
        compatibility_mode = self._compatibility_switch.get_active()
        if compatibility_mode:
            self._bluez_row.set_subtitle(
                _("Not required in compatibility mode")
            )
        elif not compatibility.notifications_supported:
            self._bluez_row.set_subtitle(
                _("Not required; per-app notifications are unsupported")
            )
        else:
            self._bluez_row.set_subtitle(
                _("Active") if active else _("A one-time Bluetooth restart is required")
            )
        self._activate_button.set_visible(
            not compatibility_mode
            and compatibility.notifications_supported
            and not active
        )

    def _selected_adapter_name(self) -> str:
        if self._compatibility is None:
            return ""
        return self._compatibility.adapter

    def _apply_compatibility(self, compatibility) -> None:
        self._compatibility = compatibility
        adapters = list(compatibility.adapters)
        self._applying_adapter = True
        if len(adapters) > 1:
            self._hardware_row.set_visible(False)
            self._adapter_row.set_visible(True)
            labels = [option.label for option in adapters]
            self._adapter_model.splice(0, self._adapter_model.get_n_items(), labels)
            selected = next(
                (index for index, option in enumerate(adapters)
                 if option.name == compatibility.adapter),
                0,
            )
            self._adapter_row.set_selected(selected)
        else:
            self._adapter_row.set_visible(False)
            self._hardware_row.set_visible(True)
            if not compatibility.available:
                hardware = _("Capabilities could not be verified; pairing is still available")
            elif not compatibility.hardware_supported:
                hardware = _("Compatibility warning; pairing is still available")
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
        self._applying_adapter = False
        self._applying_pairing_mode = True
        self._compatibility_switch.set_active(
            self._compatibility_override
            or not compatibility.notifications_supported
        )
        self._applying_pairing_mode = False
        self._apply_bluetooth_support_status(compatibility)

    def _adapter_changed(self, *_args) -> None:
        if self._applying_adapter or self._compatibility is None:
            return
        adapters = list(self._compatibility.adapters)
        index = self._adapter_row.get_selected()
        if index >= len(adapters):
            return
        name = adapters[index].name
        if name == self._compatibility.adapter:
            return

        def loaded(compatibility) -> None:
            had_devices = bool(self._devices)
            self._apply_compatibility(compatibility)
            self._set_pairing_busy(False)
            self._update_onboarding()
            self._load_devices(scan=False)
            if had_devices:
                self._toast(_("Scan again to find an iPhone on this controller"))

        self._run_setup(lambda: self._setup.compatibility(name), loaded)

    def _run_setup(self, operation, on_done) -> None:
        """Run blocking BlueZ setup work away from GTK's main loop."""
        self._set_pairing_busy(True)

        self._setup_runner.run(
            operation,
            lambda value: self._setup_completed(on_done, value),
            self._setup_failed,
        )

    def _refresh_issue_offer(self, report_path: str = "") -> None:
        path = report_path.strip()
        if not path:
            found = issue_report()
            path = str(found) if found is not None else ""
        self._pairing_issue_report = path
        self._issue_group.set_visible(bool(path))

    def _file_pairing_issue(self, _button) -> None:
        path = self._pairing_issue_report
        if not path:
            return
        dialog = Adw.AlertDialog(
            heading=_("Report Pairing Issue"),
            body=_(
                "A pairing report was saved at {path}. Attach that file to a "
                "GitHub issue and include the iPhone model and iOS version."
            ).format(path=path),
            heading_use_markup=False,
            body_use_markup=False,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("open", _("Open GitHub"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)

        def responded(_current, response: str) -> None:
            if response != "open":
                return
            try:
                Gio.AppInfo.launch_default_for_uri(issue_url(path), None)
            except Exception:
                self._toast(_("Could not open GitHub. The report is at {path}.").format(path=path))

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _setup_failed(self, message: str) -> bool:
        self._set_pairing_busy(False)
        self._refresh_issue_offer()
        self._toast(_("Setup failed: {error}").format(error=message))
        return False

    def _setup_completed(self, on_done, value) -> bool:
        self._set_pairing_busy(False)
        on_done(value)
        return False

    def _load_setup_state(self, *, scan_after: bool = False) -> None:
        def operation():
            adapter = self._selected_adapter_name() or None
            return self._setup.compatibility(adapter), self._setup.configuration()

        def loaded(value) -> None:
            compatibility, configuration = value
            self._configuration = configuration
            self._setup_loaded = True
            self._apply_compatibility(compatibility)
            self._set_pairing_busy(False)
            self._update_phone_controls()
            # Configuration and daemon status load independently. Re-render
            # status-dependent setup guidance after both sides are available.
            self._apply_status(self._last_status)
            self._update_onboarding()
            self._refresh_issue_offer(configuration.pairing_issue_report)
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
            selected_adapter = self._compatibility.adapter if self._compatibility else ""
            self._devices = iphone_candidates(
                devices,
                adapter=selected_adapter,
                configured_mac=(self._configuration.mac if self._configuration else ""),
                include_unpaired=scan,
            )
            labels = []
            for item in self._devices:
                if item.paired:
                    template = _("{name} — {mac} (paired)")
                else:
                    template = _("{name} — {mac}")
                labels.append(template.format(name=item.name, mac=item.mac))
            self._device_model.splice(
                0,
                self._device_model.get_n_items(),
                labels,
            )
            self._device_row.set_selected(0 if self._devices else Gtk.INVALID_LIST_POSITION)
            self._set_pairing_busy(False)
            self._update_phone_controls()
            self._update_onboarding()
            if scan and not self._devices:
                self._toast(
                    _(
                        "No Bluetooth devices found; unlock the iPhone and keep "
                        "Bluetooth settings open"
                    )
                )

        adapter = self._selected_adapter_name() or None
        self._run_setup(
            lambda: self._setup.devices(
                scan_seconds=DISCOVERY_SECONDS if scan else 0, adapter=adapter,
            ),
            loaded,
        )

    def _complete_pairing(self, _button) -> None:
        device = self._selected_device()
        if not device:
            self._toast(_("Scan for and select an iPhone first"))
            return
        configuration = self._configuration
        if device.paired or configuration is None or not configuration.saved:
            self._start_pairing(device)
            return

        dialog = Adw.AlertDialog(
            heading=_("Replace the Saved iPhone?"),
            body=_(
                "Pairing this iPhone will remove BlueFerry's saved phone and "
                "its local Bluetooth bond. Before continuing, also forget this "
                "computer in the old iPhone's Bluetooth settings."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("replace", _("Replace and Pair"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance(
            "replace", Adw.ResponseAppearance.DESTRUCTIVE
        )

        def responded(_current, response: str) -> None:
            if response == "replace":
                self._start_pairing(
                    device,
                    replace_saved_mac=configuration.mac,
                )

        dialog.connect("response", responded)
        dialog.present(self.get_root())

    def _start_pairing(
        self,
        device: PairedDevice,
        *,
        replace_saved_mac: str = "",
    ) -> None:
        self._toast(_("Activating Bluetooth, then starting secure pairing…"))

        def completed(result) -> None:
            self._configuration = ConfigurationState(
                configured=True,
                mac=device.mac,
                adapter=device.adapter_path.rsplit("/", 1)[-1],
                path="",
                saved=True,
                bonded=True,
                ancs_enabled=result.ancs_enabled,
            )
            if result.ancs_enabled and not result.ancs_ready:
                message = _(
                    "Pairing is complete; iPhone notification access is still "
                    "settling. Keep Bluetooth settings open."
                )
            else:
                message = _("Linux setup is complete; finish the two iPhone settings")
            self._toast(message)
            self._refresh_issue_offer()
            self._load_devices(scan=False)
            self._update_onboarding()
            self._refresh()

        def confirm(passkey: int | None) -> bool:
            decided = threading.Event()
            accepted = False

            def present_confirmation() -> bool:
                dialog = Adw.AlertDialog(
                    heading=(
                        _("Do the Bluetooth codes match?")
                        if passkey is not None
                        else _("Approve Bluetooth pairing?")
                    ),
                    body=(
                        _("Confirm that {passkey} is shown on both this computer "
                          "and the iPhone.").format(passkey=f"{passkey:06d}")
                        if passkey is not None
                        else _("Approve only if you started this pairing from BlueFerry.")
                    ),
                )
                dialog.add_response("reject", _("Cancel Pairing"))
                dialog.add_response(
                    "accept",
                    _("Codes Match") if passkey is not None else _("Approve Pairing"),
                )
                dialog.set_close_response("reject")
                dialog.set_response_appearance(
                    "accept", Adw.ResponseAppearance.SUGGESTED
                )

                def responded(_current, response: str) -> None:
                    nonlocal accepted
                    accepted = response == "accept"
                    decided.set()

                dialog.connect("response", responded)
                dialog.present(self.get_root())
                return GLib.SOURCE_REMOVE

            GLib.idle_add(present_confirmation)
            # BlueZ's agent request timeout is 60 seconds. Return a secure
            # rejection if the window disappears or the UI never answers.
            return decided.wait(60.0) and accepted

        def display(passkey: int) -> None:
            GLib.idle_add(
                self._toast,
                _("Bluetooth pairing code: {passkey}").format(
                    passkey=f"{passkey:06d}"
                ),
            )

        self._run_setup(
            lambda: self._setup.complete_isolated(
                device.mac,
                confirmation=confirm,
                display=display,
                adapter=device.adapter_path.rsplit("/", 1)[-1],
                replace_saved_mac=replace_saved_mac,
                compatibility_mode=self._compatibility_switch.get_active(),
                explicit_pairing=self._explicit_pairing_switch.get_active(),
            ),
            completed,
        )

    def _confirm_forget(self, _button) -> None:
        device = self._configured_device() or self._selected_device()
        mac = (
            self._configuration.mac
            if self._configuration and self._configuration.configured
            else device.mac
            if device
            else ""
        )
        if not mac:
            return
        dialog = Adw.AlertDialog(
            heading=_("Unpair {name}?").format(name=device.name if device else _("this iPhone")),
            body=_(
                "For a clean re-pair, also forget this computer in the iPhone's Bluetooth settings."
            ),
            heading_use_markup=False,
            body_use_markup=False,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("forget", _("Unpair"))
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)

        def responded(_current, response: str) -> None:
            if response == "forget":

                def forgotten(_value) -> None:
                    self._configuration = ConfigurationState(
                        configured=False,
                        mac="",
                        adapter=(self._compatibility.adapter if self._compatibility else ""),
                        path="",
                    )
                    self._apply_status(BackendStatus())
                    self._refresh_issue_offer()
                    self._load_devices(scan=False)
                    self._update_phone_controls()

                self._run_setup(
                    lambda: self._setup.forget(
                        mac,
                        adapter=(
                            self._configuration.adapter
                            if self._configuration
                            else None
                        ) or None,
                    ),
                    forgotten,
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
        self._daemon_row.set_subtitle(connection_subtitle(values, reachable=reachable))
        self._daemon_icon.set_from_icon_name(
            "emblem-ok-symbolic" if reachable else "dialog-warning-symbolic"
        )

        healthy = status.map
        if map_connection_refused(values):
            map_subtitle = map_connection_refused_message()
        elif healthy:
            map_subtitle = _("Connected")
        else:
            map_subtitle = _("Unavailable — Check the iPhone Settings Below")
        self._map_row.set_subtitle(map_subtitle)
        self._map_icon.set_from_icon_name(
            "emblem-ok-symbolic" if healthy else "dialog-warning-symbolic"
        )

        pbap_connected = bool(values.get("pbap"))
        self._pbap_row.set_subtitle(_("Connected") if pbap_connected else _("Unavailable"))
        self._pbap_icon.set_from_icon_name(
            "emblem-ok-symbolic" if pbap_connected else "dialog-warning-symbolic"
        )

        ancs_connected = bool(values.get("ancs"))
        ancs_expected = bool(
            self._configuration
            and self._configuration.configured
            and self._configuration.ancs_enabled
            and (
                self._compatibility is None
                or self._compatibility.notifications_supported
            )
        )
        show_ancs_recovery = bool(
            status.map and status.pbap and not ancs_connected and ancs_expected
        )
        ancs_subtitle = _("Connected") if ancs_connected else _("Unavailable")
        self._ancs_row.set_subtitle(ancs_subtitle)
        self._ancs_icon.set_from_icon_name(
            "emblem-ok-symbolic" if ancs_connected else "dialog-warning-symbolic"
        )
        self._ancs_recovery_label.set_visible(show_ancs_recovery)
        self._contacts_row.set_subtitle(str(status.contacts))
        self._events_row.set_subtitle(str(status.events))
        policy = status.notification_policy
        selected = {"all": 0, "messages": 1, "none": 2}.get(policy, 1)
        self._applying_notification_policy = True
        self._notification_policy_row.set_selected(selected)
        self._notification_policy_row.set_sensitive(reachable)
        self._applying_notification_policy = False
        self._applying_storage_policy = True
        selected_storage = {
            "encrypted": 0,
            "plaintext": 1,
            "none": 2,
        }.get(status.storage_policy, 0)
        self._storage_policy_row.set_selected(selected_storage)
        self._storage_policy_row.set_sensitive(reachable)
        self._applying_storage_policy = False
        locked = status.storage_policy == "encrypted" and status.storage_state != "ready"
        if reachable and locked and not self._storage_unlock_attempted:
            self._storage_unlock_attempted = True
            self._unlock_storage()
        self._update_iphone_setup_tasks()
        self._update_onboarding()
        self._refresh_issue_offer()
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
            self._toast(_("Could not save notification preference: {error}").format(error=error))
            self._apply_status(self._last_status)

        self._client.set_notification_policy_async(policy, saved, failed)

    def _storage_policy_changed(self, _row, _property) -> None:
        if self._applying_storage_policy:
            return
        policy = {
            0: "encrypted",
            1: "plaintext",
            2: "none",
        }.get(self._storage_policy_row.get_selected())
        if policy is None or policy == self._last_status.storage_policy:
            return
        prompts = {
            "encrypted": (
                _("Use Encrypted Local Storage?"),
                _(
                    "Changing storage protection clears existing local message "
                    "history and cached contacts. New local data will be encrypted "
                    "with your desktop keyring. Nothing on the iPhone is deleted."
                ),
                _("Clear and Use Encryption"),
            ),
            "plaintext": (
                _("Store Local Data Without Encryption?"),
                _(
                    "This clears existing local message history and cached contacts. "
                    "New local data will be stored unencrypted and can be read by "
                    "anyone with access to your files. Nothing on the iPhone is deleted."
                ),
                _("Clear and Store Unencrypted"),
            ),
            "none": (
                _("Stop Retaining Local Data?"),
                _(
                    "This clears message history and cached contacts, then removes "
                    "BlueFerry's storage key. Nothing on the iPhone is deleted."
                ),
                _("Clear and Stop Retaining"),
            ),
        }
        heading, body, confirm_label = prompts[policy]
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("confirm", confirm_label)
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect(
            "response",
            lambda _dialog, response: (
                self._save_storage_policy(policy)
                if response == "confirm"
                else self._apply_status(self._last_status)
            ),
        )
        dialog.present(self.get_root())

    def _save_storage_policy(self, policy: str) -> None:
        self._storage_policy_row.set_sensitive(False)

        def saved(_value: dict) -> None:
            self._toast(
                _(
                    "Local data will not be retained"
                    if policy == "none"
                    else "Unencrypted local storage enabled"
                    if policy == "plaintext"
                    else "Encrypted local storage enabled"
                )
            )
            self._refresh()

        def failed(error: str) -> None:
            self._toast(_("Could not change local storage: {error}").format(error=error))
            self._apply_status(self._last_status)

        self._client.set_storage_policy_async(policy, saved, failed)

    def _unlock_storage(self) -> None:
        self._client.unlock_storage_async(
            lambda _value: self._refresh(),
            lambda error: (
                self._toast(_("Could not unlock storage: {error}").format(error=error)),
                self._refresh(),
            ),
        )

    def _update_onboarding(self) -> None:
        compatibility = self._compatibility.to_dict() if self._compatibility else {}
        configured = bool(self._configuration and self._configuration.configured)
        if self._compatibility_switch.get_active() or (
            configured and self._configuration and not self._configuration.ancs_enabled
        ):
            compatibility["notifications_supported"] = False
        stage = derive_stage(
            setup_loaded=self._setup_loaded,
            configured=configured,
            compatibility=compatibility,
            status=self._last_status,
        )
        if stage in {
            OnboardingStage.READY,
            OnboardingStage.READY_WITHOUT_ANCS,
        } and self._last_onboarding_stage not in {
            OnboardingStage.READY,
            OnboardingStage.READY_WITHOUT_ANCS,
        }:
            self._toast(_("BlueFerry is connected and ready"))
        self._last_onboarding_stage = stage

    def _sync_contacts(self, _button) -> None:
        self._client.sync_contacts(
            on_ok=lambda count: (
                self._toast(_("Synced {count} contact destinations").format(count=count)),
                self._refresh(),
            ),
            on_err=lambda error: self._toast(_("Contact sync failed: {error}").format(error=error)),
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
                lambda error: self._toast(_("Clear failed: {error}").format(error=error)),
            )

        dialog.connect("response", responded)
        dialog.present(self.get_root())
