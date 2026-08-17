"""Installed identities agree with the public runtime protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from blueferry.protocol import BUS_NAME, OBJECT_PATH

ROOT = Path(__file__).resolve().parents[1]


def _qml_bundle(directory: Path) -> str:
    """Return a domain bundle without coupling tests to one root filename."""
    return "\n".join(
        path.read_text() for path in sorted(directory.glob("*.qml"))
    )


def test_graphical_commands_follow_client_package_names() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    desktop = (ROOT / "data" / "io.weirdware.BlueFerry.Gtk.desktop").read_text()

    assert 'blueferry-gtk = "blueferry.ui.app:main"' in project
    assert "/usr/bin/blueferry-gtk" in pkgbuild
    assert "Exec=blueferry-gtk" in desktop
    assert "blueferry-ui" not in project + pkgbuild + desktop


def test_tui_entry_point_is_shipped_by_arch_backend() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    backend = pkgbuild.split("package_blueferry-backend()", 1)[1].split(
        "package_blueferry-gtk()", 1
    )[0]
    package_names = pkgbuild.split("pkgname=(", 1)[1].split("\n)", 1)[0]

    assert 'blueferry-tui = "blueferry.tui_launcher:main"' in project
    assert '"textual>=8.0"' in project
    assert '"blueferry" = ["tui.tcss"]' in project
    assert "blueferry-tui" not in package_names
    assert "package_blueferry-tui()" not in pkgbuild
    assert "conflicts=('blueferry-tui')" in backend
    assert "provides=('blueferry-tui')" in backend
    assert "replaces=('blueferry-tui')" in backend
    assert "'python-textual>=8.0'" in backend
    assert "blueferry/tui.py" not in backend
    assert "blueferry/tui.tcss" not in backend
    assert '$_stage/usr/bin/blueferry-tui' in backend
    assert '$pkgdir/usr/bin/blueferry-tui' in backend


def test_quickshell_bridge_entry_point_is_shipped_by_arch_backend() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    backend = pkgbuild.split("package_blueferry-backend()", 1)[1].split(
        "package_blueferry-gtk()", 1
    )[0]

    assert (
        'blueferry-quickshell-bridge = "blueferry.quickshell_bridge:main"'
        in project
    )
    assert '$_stage/usr/bin/blueferry-quickshell-bridge' in backend
    assert '$pkgdir/usr/bin/blueferry-quickshell-bridge' in backend


def test_dbus_activation_and_systemd_publish_the_runtime_bus_name() -> None:
    activation = (ROOT / "packaging" / "arch" / f"{BUS_NAME}.service").read_text()
    unit = (ROOT / "systemd" / "blueferry.service").read_text()

    assert f"Name={BUS_NAME}" in activation
    assert f"BusName={BUS_NAME}" in unit
    assert OBJECT_PATH == "/" + BUS_NAME.replace(".", "/")


def test_backend_autostart_is_package_owned_and_configuration_gated() -> None:
    unit = (ROOT / "systemd" / "blueferry.service").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()

    assert "ConditionPathExists=%h/.config/blueferry/local.env" in unit
    assert "[Install]" not in unit
    assert 'default.target.wants/blueferry.service"' in pkgbuild
    assert "ln -s ../blueferry.service" in pkgbuild


def test_backend_unit_does_not_source_user_controlled_process_environment() -> None:
    unit = (ROOT / "systemd" / "blueferry.service").read_text()

    assert "EnvironmentFile=" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit


def test_backend_user_unit_does_not_set_a_capability_bounding_set() -> None:
    unit = (ROOT / "systemd" / "blueferry.service").read_text()

    assert "CapabilityBoundingSet=" not in unit


def test_backend_package_includes_maintained_protocol_findings() -> None:
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()

    assert "install -Dm644 PROTOCOL.md" in pkgbuild
    assert not (ROOT / "spike").exists()


def test_arch_check_dependencies_cover_cli_test_imports() -> None:
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    checkdepends = pkgbuild.split("checkdepends=(", 1)[1].split("\n)", 1)[0]

    assert "'python-typer>=0.12'" in checkdepends
    assert "'python-textual>=8.0'" in checkdepends
    assert "'python-coverage'" in checkdepends


def test_repository_quality_workflow_runs_hermetic_checks() -> None:
    workflow = (ROOT / ".github/workflows/quality.yml").read_text()

    assert "dbus-run-session --config-file=tests/dbus-test.conf" in workflow
    assert "python -m coverage run -m pytest" in workflow
    assert "mypy src/blueferry" in workflow
    assert "qmllint src/blueferry/qt/qml/*.qml data/quickshell/*.qml" in workflow


def test_arch_bluetooth_dropin_warns_about_other_executable_paths() -> None:
    dropin = (ROOT / "packaging/arch/blueferry-bluetooth.conf").read_text()

    assert "Arch Linux only" in dropin
    assert "/usr/lib/bluetooth/bluetoothd" in dropin
    assert "/usr/libexec/bluetooth/bluetoothd" in dropin
    assert "prevents Bluetooth from starting" in dropin


def test_gtk_client_styles_messages_with_libadwaita_1_5_colors() -> None:
    app = (ROOT / "src/blueferry/ui/app.py").read_text()
    pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()

    assert "@accent_bg_color" in app
    assert "@accent_fg_color" in app
    assert "var(--accent-bg-color)" not in app
    assert "'libadwaita>=1.5'" in pkgbuild


@pytest.mark.parametrize("suffix", ["Gtk", "Qt", "Quickshell"])
def test_desktop_and_appstream_ids_match(suffix: str) -> None:
    app_id = f"{BUS_NAME}.{suffix}"
    desktop = (ROOT / "data" / f"{app_id}.desktop").read_text()
    metainfo = (ROOT / "data" / f"{app_id}.metainfo.xml").read_text()

    assert f"Icon={BUS_NAME}" in desktop
    assert f"<id>{app_id}</id>" in metainfo
    assert f">{app_id}.desktop</launchable>" in metainfo
    assert "Telephony" not in desktop


def test_native_backend_owns_the_shared_application_icon() -> None:
    icon = "icons/hicolor/scalable/apps/io.weirdware.BlueFerry.svg"
    arch = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    arch_backend = arch.split("package_blueferry-backend()", 1)[1].split(
        "package_blueferry-gtk()", 1
    )[0]
    arch_clients = arch.split("package_blueferry-gtk()", 1)[1]
    rpm = (ROOT / "packaging" / "rpm" / "blueferry.spec").read_text()
    rpm_backend = rpm.split("%files\n", 1)[1].split(
        "%files -n blueferry-gtk", 1
    )[0]
    rpm_clients = rpm.split("%files -n blueferry-gtk", 1)[1]
    deb = ROOT / "packaging" / "deb"

    assert icon in arch_backend
    assert icon not in arch_clients
    assert icon in rpm_backend
    assert icon not in rpm_clients
    assert icon in (deb / "blueferry-backend.install").read_text()
    assert icon not in (deb / "blueferry-gtk.install").read_text()
    assert icon not in (deb / "blueferry-qt.install").read_text()


def test_qt_package_ships_the_kirigami_ui_and_dependencies() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()
    qml = _qml_bundle(ROOT / "src/blueferry/qt/qml")

    assert '"blueferry.qt" = ["qml/*.qml"]' in project
    assert "'kirigami'" in pkgbuild
    assert "'qqc2-desktop-style'" in pkgbuild
    assert "Kirigami.ApplicationWindow" in qml
    assert "Kirigami.NavigationTabBar" not in qml
    assert "pageStack.push(iphonePageLoader.item)" in qml
    assert "sourceComponent: iphonePageComponent" in qml
    assert "root.pageStack.layers.push(aboutPage)" in qml
    assert qml.count("pageStack.layers.push") == 1
    assert "Controls.StackView.Immediate" not in qml
    assert "Kirigami.AboutPage" in qml
    assert "customFooterActions" in qml
    assert "interval: 3000" not in qml
    qt_app = (ROOT / "src" / "blueferry" / "qt" / "app.py").read_text()
    assert f'APP_ICON = "{BUS_NAME}"' in qt_app
    assert "setDesktopFileName(APP_ID)" in qt_app
    assert "setWindowIcon(QIcon.fromTheme(APP_ICON))" in qt_app
    assert "QSystemTrayIcon" in qt_app
    assert 'QIcon.fromTheme("smartphone-symbolic")' in qt_app
    assert "setQuitOnLastWindowClosed(False)" in qt_app


def test_gui_pairing_requires_confirmation_before_replacing_saved_target() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "Replace" in client
        assert "saved phone" in client
        assert "old iPhone's Bluetooth settings" in client
    assert "replaceAndPair" in qt
    assert "--replace-saved-mac" in quickshell


def test_gui_pairing_guidance_tells_users_to_recheck_iphone_toggles() -> None:
    clients = (
        (ROOT / "src/blueferry/ui/status.py").read_text(),
        _qml_bundle(ROOT / "src/blueferry/qt/qml"),
        _qml_bundle(ROOT / "data/quickshell"),
    )

    for client in clients:
        assert "this computer's ⓘ page a" in client
        assert "few times; turn on any new toggles that appear" in client


def test_all_clients_offer_bluez_restart_as_ancs_repair_recovery() -> None:
    clients = (
        (ROOT / "src/blueferry/ui/status.py").read_text(),
        _qml_bundle(ROOT / "src/blueferry/qt/qml"),
        _qml_bundle(ROOT / "data/quickshell"),
        (ROOT / "src/blueferry/tui.py").read_text(),
        (ROOT / "src/blueferry/pairing_cli.py").read_text(),
    )

    for client in clients:
        assert "ANCS remains unavailable" in client
        assert "sudo systemctl restart " in client
        assert "bluetooth.service" in client
        assert "forget this computer on the iPhone and" in client
        assert "pair again" in client
        assert "briefly disconnects all Bluetooth devices" in client


def test_all_pairing_clients_expose_compatibility_mode() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "Compatibility pairing" in client
        assert "iOS 18 or earlier" in client
    assert "compatibility_mode=" in gtk
    assert "compatibilityMode.checked" in qt
    assert "text: compatibilityMode.checked" in qt
    assert '"--compatibility-mode"' in quickshell


def test_all_pairing_clients_expose_explicit_pairing_mode() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "Use explicit Bluetooth pairing" in client
    assert "explicit_pairing=" in gtk
    assert "explicitPairing.checked" in qt
    assert '"--explicit-pairing"' in quickshell


def test_capability_checks_do_not_disable_pairing_buttons() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "self._pair_button.set_sensitive(not busy and bool(selected))" in gtk
    assert "root.bridge.compatibility.pairing_ready" not in qt
    assert "root.bridge.compatibility.messages_supported" not in qt.split(
        'text: iphonePage.device !== null', 1
    )[1].split("onClicked:", 1)[0]
    assert "root.bridge.compatibilityLoaded" in qt.split(
        'text: iphonePage.device !== null', 1
    )[1].split("onClicked:", 1)[0]
    assert "root.pairingReady" not in quickshell.split(
        'text: pairProcess.running ? "Pairing…"', 1
    )[1].split("onClicked:", 1)[0]
    assert "root.messagesSupported" not in quickshell.split(
        'text: pairProcess.running ? "Pairing…"', 1
    )[1].split("onClicked:", 1)[0]
    assert "root.compatibilityLoaded" in quickshell.split(
        'text: pairProcess.running ? "Pairing…"', 1
    )[1].split("onClicked:", 1)[0]


def test_gtk_connection_rows_all_have_status_icons() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()

    for profile in ("daemon", "map", "pbap", "ancs"):
        assert f"self._{profile}_icon = Gtk.Image()" in gtk
        assert f"self._{profile}_row.add_suffix(self._{profile}_icon)" in gtk
        row_definition = gtk.split(
            f"self._{profile}_row = Adw.ActionRow(", 1
        )[1].split(f"self._{profile}_icon", 1)[0]
        assert "use_markup=False" in row_definition
    assert "self._ancs_recovery_label = Gtk.Label(" in gtk
    assert "self._ancs_recovery_label.set_visible(show_ancs_recovery)" in gtk
    assert "self._apply_status(self._last_status)" in gtk


def test_qt_message_bubbles_do_not_use_full_selection_color() -> None:
    bubble = (ROOT / "src/blueferry/qt/qml/MessageBubble.qml").read_text()

    assert "backgroundColor.r * 0.78" in bubble
    assert "highlightColor.r * 0.22" in bubble
    assert "? Kirigami.Theme.highlightColor" not in bubble
    assert "Kirigami.Theme.highlightedTextColor" not in bubble


def test_quickshell_outgoing_bubbles_match_qt_accent_tint() -> None:
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "? theme.selectedSurface : theme.raisedSurface" in quickshell
    assert "color: theme.windowText" in quickshell


def test_group_message_bubbles_show_the_individual_sender() -> None:
    gtk = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    qt_bubble = (ROOT / "src/blueferry/qt/qml/MessageBubble.qml").read_text()
    qt_view = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert 'label=_("You") if message.outgoing else message.sender' in gtk
    assert 'text: root.message.outgoing ? qsTr("You")' in qt_bubble
    assert "&& messagesPage.thread.is_group" in qt_view
    assert '? "You" : (messageRow.modelData.sender || "")' in quickshell


def test_group_roster_editors_explain_local_and_same_name_limits() -> None:
    clients = (
        (ROOT / "src/blueferry/ui/conversations.py").read_text(),
        _qml_bundle(ROOT / "src/blueferry/qt/qml"),
        _qml_bundle(ROOT / "data/quickshell"),
        (ROOT / "src/blueferry/tui.py").read_text(),
    )

    for client in clients:
        lowered = client.casefold()
        assert "multiple groups" in lowered
        assert "does not add or remove anyone" in lowered
        assert "group membership may have changed" in lowered
        assert "roster_changed" in client


def test_qt_escaped_roster_names_are_forced_to_rich_text() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert 'return "<span>" + value + "</span>"' in qml
    assert "root.escapedRichText(" in qml
    assert "root.htmlEscape(thread.name)" in qml


def test_qt_messages_toolbar_toggles_dismissible_settings_pane() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert 'text: qsTr("Settings")' in qml
    assert "onClicked: root.togglePhoneSettings()" in qml
    assert 'text: qsTr("Close Settings")' in qml
    assert "onClicked: root.closePhoneSettings()" in qml
    assert "pageStack.push(iphonePageLoader.item)" in qml
    assert "pageStack.removePage(page)" in qml
    assert "messagesPage.thread.group_origin" in qml
    assert "messagesPage.actions" not in qml
    assert "iphonePage.actions" not in qml
    assert 'text: qsTr("Refresh")' not in qml


def test_qt_connection_health_stays_compact_and_centered() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    health = qml.split('text: qsTr("Connection Health")', 1)[1]
    health = health.split('text: qsTr("Desktop Notifications")', 1)[0]

    assert "Layout.fillWidth: root.mapConnectionRefused()" in health
    assert 'Kirigami.FormData.label: qsTr("ANCS Recovery:")' not in health
    assert "Kirigami.InlineMessage" in health


def test_qt_sends_group_replies_without_a_confirmation_dialog() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert "messagesPage.thread.is_group" in qml
    assert 'title: qsTr("Send Group Message?")' not in qml
    assert "groupDialog.open()" not in qml
    assert "pendingThread" not in qml
    assert "pendingBody" not in qml


def test_qt_conversation_panes_start_compact_and_are_resizable() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert "Controls.SplitView {" in qml
    assert "messagesSplit.width * 0.35" in qml
    assert "Controls.SplitView.fillWidth: true" in qml
    assert "handle: Item {" in qml


def test_gui_clients_keep_phone_settings_out_of_primary_navigation() -> None:
    gtk_window = (ROOT / "src/blueferry/ui/window.py").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "Adw.ViewSwitcher" not in gtk_window
    assert 'menu.append(_("iPhone Settings"), "win.phone")' in gtk_window
    assert 'Accessible.name: "iPhone settings"' in quickshell
    assert 'text: "Messages"' not in quickshell
    assert 'text: "iPhone"' not in quickshell


def test_all_gui_clients_offer_contacts_aware_new_messages() -> None:
    gtk = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    qt_controller = (ROOT / "src/blueferry/qt/controller.py").read_text()
    qt_qml = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    assert 'tooltip_text=_("New Message")' in gtk
    assert "find_contacts_async" in gtk
    assert "def findContacts" in qt_controller
    assert 'text: qsTr("New Message")' in qt_qml
    assert 'Accessible.name: "New message"' in quickshell
    assert 'backendBridge.request("contacts"' in quickshell


def test_all_clients_expand_and_scroll_long_message_drafts() -> None:
    gtk = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()
    tui_styles = (ROOT / "src/blueferry/tui.tcss").read_text()

    assert gtk.count("= MessageComposer(") == 2
    assert "Gtk.Overlay(child=self._view)" in gtk
    assert "placeholder_text=placeholder" not in gtk
    assert "propagate_natural_height=True" in gtk
    assert "vscrollbar_policy=Gtk.PolicyType.AUTOMATIC" in gtk
    assert "follows_content_size=True" in gtk
    assert "Gdk.ModifierType.SHIFT_MASK" in gtk
    assert gtk.count("wrap_mode=Pango.WrapMode.WORD_CHAR") == 2
    message_viewport = gtk.split("self._msg_scroll = Gtk.ScrolledWindow(", 1)[1]
    assert "hscrollbar_policy=Gtk.PolicyType.NEVER" in message_viewport.split(
        ")", 1
    )[0]

    assert qt.count("ExpandingMessageComposer {") == 2
    assert "Layout.minimumWidth: 0" in qt
    assert "Controls.ScrollBar.vertical.policy: Controls.ScrollBar.AsNeeded" in qt
    assert "Layout.maximumHeight: Kirigami.Units.gridUnit * 8" in qt
    assert "Qt.ShiftModifier" in qt

    assert quickshell.count("FerryMessageComposer {") == 2
    assert "Layout.minimumWidth: 0" in quickshell
    assert "ScrollBar.vertical.policy: ScrollBar.AsNeeded" in quickshell
    assert "Layout.maximumHeight: theme.scaled(144)" in quickshell
    assert "Qt.ShiftModifier" in quickshell

    composer_styles = tui_styles.split("#composer {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    assert "height: auto" in composer_styles
    assert "max-height: 8" in composer_styles


def test_quickshell_keeps_private_dbus_values_out_of_process_arguments() -> None:
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    assert 'command: ["/usr/bin/blueferry-quickshell-bridge"]' in quickshell
    assert 'backendBridge.request("send"' in quickshell
    assert 'backendBridge.request("send_to_thread"' in quickshell
    assert 'backendBridge.request("set_group_participants"' in quickshell
    assert 'backendBridge.request("delete_threads"' in quickshell
    for cli_adapter in (
        '"contacts-json"',
        '"message-send"',
        '"thread-send"',
        '"group-participants-set"',
        '"notification-policy-set"',
        '"storage-policy-set"',
        '"storage-unlock"',
        '"status-json"',
        '"threads-json"',
        '"/usr/bin/dbus-monitor"',
    ):
        assert cli_adapter not in quickshell


def test_graphical_clients_offer_backend_owned_context_menu_delete() -> None:
    gtk = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "Gdk.BUTTON_SECONDARY" in gtk
    assert 'label=_("Delete Conversation")' in gtk
    assert "delete_threads_async(\n                [thread_key]" in gtk
    assert "Controls.Menu {" in qt
    assert "onClicked: root.selectedThreadKey = modelData.key" in qt
    assert "acceptedButtons: Qt.RightButton" in qt
    assert "root.bridge.deleteThreads([deleteThreadsDialog.threadKey])" in qt
    assert "Menu {" in quickshell
    assert "root.selectedThreadKey = modelData.key" in quickshell
    assert "acceptedButtons: Qt.RightButton" in quickshell
    assert 'backendBridge.request("delete_threads"' in quickshell
    assert "thread_keys: [deleteThreadsPopup.threadKey]" in quickshell
    for source in (gtk, qt, quickshell):
        assert "SelectionMode.MULTIPLE" not in source
        assert "threadSelectionMode" not in source
        assert "selectedThreadKeys" not in source


def test_all_gui_clients_can_choose_a_bluetooth_controller() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    assert "_adapter_row" in gtk
    assert "selectAdapter" in qt
    assert "Bluetooth Controller" in gtk
    assert "Controller:" in qt
    assert "adapterCombo" in quickshell
    assert "--adapter" in quickshell
    assert 'pairProcess.command.push("--adapter", root.adapterName)' in quickshell
    assert 'forgetProcess.command.push("--adapter", root.configuredAdapter)' in quickshell
    assert '"--interactive-approval"' in quickshell
    assert "forgetProcess.write(\"yes\\n\")" in quickshell
    assert "property string configuredAdapter" in quickshell
    assert (
        "root.configuredAdapter = root.targetSaved ? (parsed.adapter || \"\") : \"\""
    ) in quickshell
    assert "if (root.targetSaved && parsed.adapter)" not in quickshell
    assert "scanAfterCompatibility" in quickshell
    assert 'command.push("--scan-seconds", "24")' in quickshell
    assert "scan_seconds=DISCOVERY_SECONDS if scan else 0" in (
        ROOT / "src/blueferry/ui/status.py"
    ).read_text()
    assert "scan_seconds=DISCOVERY_SECONDS if scan else 0" in (
        ROOT / "src/blueferry/qt/controller.py"
    ).read_text()


def test_all_gui_clients_offer_a_pairing_issue_button() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "Report Pairing Issue" in client
        assert "iPhone model" in client
        assert "iOS version" in client
    assert "blueferry pairing-issue" in (ROOT / "src/blueferry/quirks_report.py").read_text()
    assert '"pairing-issue"' in quickshell
    assert '"--print-url"' in quickshell


def test_all_gui_clients_explain_desktop_initiated_pairing() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "select your iPhone here, then choose Pair" in client
        assert "pairing request appears on the iPhone" in client
        assert "Other Devices" not in client


def test_all_gui_clients_explain_map_connection_refusal() -> None:
    message = (
        "iPhone is refusing message connections; is it connected to another computer?"
    )
    gtk_messages = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    gtk_settings = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    assert "map_connection_refused_message" in gtk_messages
    assert "map_connection_refused_message" in gtk_settings
    assert qt.count(message) >= 2
    assert quickshell.count(message) >= 2


def test_all_gui_clients_offer_unencrypted_local_storage() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    assert '_("Unencrypted Local Data")' in gtk
    assert '"value": "plaintext"' in qt
    assert '"Unencrypted local data"' in quickshell


def test_gui_clients_handle_encrypted_storage_unlocks() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt_controller = (ROOT / "src/blueferry/qt/controller.py").read_text()
    qt_qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "_unlock_storage_button" not in gtk
    assert "_maybe_unlock_storage" in qt_controller
    assert "onClicked: root.bridge.unlockStorage()" in qt_qml
    assert 'qsTr("Conversation History Unavailable")' in qt_qml
    assert 'qsTr("Unlock Local Data")' in qt_qml
    assert "root.maybeUnlockStorage()" in quickshell
    assert "onClicked: storageUnlockProcess.running" not in quickshell


def test_quickshell_package_ships_its_quattro_theme_adapter() -> None:
    pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()
    theme = (ROOT / "data/quickshell/Theme.qml").read_text()
    shell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "install -Dm644 data/quickshell/*.qml" in pkgbuild
    assert (ROOT / "data/quickshell/OnboardingState.qml").is_file()
    assert "/.local/state/omarchy/current/theme" in theme
    assert "fallbackPalette" in theme
    assert "readonly property color windowSurface" in theme
    assert "readonly property color cardSurface" in theme
    assert "readonly property color primarySurface" in theme
    assert "readonly property color muted: blend(foreground, background, 0.62)" in theme
    assert "readonly property color selectedSurface" in theme
    assert "readonly property int panelRadius" in theme
    assert "readonly property int controlRadius" in theme
    assert "color: theme.windowSurface" in shell
    assert "component FerryLabel: Label" in shell
    assert (
        "component FerryLabel: Label {\n"
        "    color: theme.windowText\n"
        "    textFormat: Text.PlainText"
    ) in shell
    assert "component FerryButton: Button" in shell
    assert "theme.primarySurface" in shell
    assert "component FerrySectionLabel: FerryLabel" in shell
    window_declaration = shell.split("FloatingWindow {", 1)[1].split("Pane {", 1)[0]
    assert 'color: "transparent"' not in window_declaration


def test_remote_qml_text_is_rendered_as_plain_text() -> None:
    qt_qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert (
        "text: contactDelegate.text\n"
        "                        textFormat: Text.PlainText"
    ) in qt_qml
    assert "text: deviceCombo.displayText" not in qt_qml
    assert (
        "text: deviceOption.modelData.display_name\n"
        "                                textFormat: Text.PlainText"
    ) in qt_qml
    assert (
        "text: threadDelegate.modelData.name\n"
        "                        textFormat: Text.PlainText"
    ) in quickshell
    assert (
        "text: newContactDelegate.modelData.name\n"
        "                  textFormat: Text.PlainText"
    ) in quickshell


def test_quickshell_message_timeline_stays_at_the_latest_message() -> None:
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "property bool stickToBottom: true" in quickshell
    assert "positionViewAtEnd()" in quickshell
    assert "onThreadKeyChanged" in quickshell
    assert "onMovementStarted: stickToBottom = false" in quickshell
    assert "messageList.stickToBottom = true" in quickshell
