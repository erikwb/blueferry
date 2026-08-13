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


def test_tui_entry_point_is_shipped_by_backend_package() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    backend = pkgbuild.split("package_blueferry-backend()", 1)[1].split(
        "package_blueferry-gtk()", 1
    )[0]

    assert 'blueferry-tui = "blueferry.tui:main"' in project
    assert '"textual>=8.0"' in project
    assert '"blueferry" = ["tui.tcss"]' in project
    assert "'python-textual>=8.0'" in backend
    assert '$_stage/usr/bin/blueferry-tui' in backend
    assert '$pkgdir/usr/bin/blueferry-tui' in backend


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
    assert "CapabilityBoundingSet=" in unit
    assert "ProtectSystem=strict" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit


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

    assert f"Icon={app_id}" in desktop
    assert f"<id>{app_id}</id>" in metainfo
    assert f">{app_id}.desktop</launchable>" in metainfo
    assert "Telephony" not in desktop


def test_qt_package_ships_the_kirigami_ui_and_dependencies() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()
    qml = _qml_bundle(ROOT / "src/blueferry/qt/qml")

    assert '"blueferry.qt" = ["qml/*.qml"]' in project
    assert "'kirigami'" in pkgbuild
    assert "'qqc2-desktop-style'" in pkgbuild
    assert "Kirigami.ApplicationWindow" in qml
    assert "Kirigami.NavigationTabBar" not in qml
    assert "pageStack.push(iphonePageComponent)" in qml
    assert "root.pageStack.push(aboutPage)" in qml
    assert "pageStack.layers.push" not in qml
    assert "Controls.StackView.Immediate" not in qml
    assert "Kirigami.AboutPage" in qml
    assert "customFooterActions" in qml
    assert "interval: 3000" not in qml
    qt_app = (ROOT / "src" / "blueferry" / "qt" / "app.py").read_text()
    assert "QSystemTrayIcon" in qt_app
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
        assert "reopen this computer's ⓘ page a" in client
        assert "few times; turn on any new toggles that appear" in client


def test_gtk_connection_rows_all_have_status_icons() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()

    for profile in ("daemon", "map", "pbap", "ancs"):
        assert f"self._{profile}_icon = Gtk.Image()" in gtk
        assert f"self._{profile}_row.add_suffix(self._{profile}_icon)" in gtk


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

    assert 'label=_("You") if msg["outgoing"] else msg.get("sender", "")' in gtk
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


def test_qt_group_confirmation_preserves_escaped_recipient_lines() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert 'replace(/\\n/g, "<br/>")' in qml
    assert "? root.escapedRichTextWithBreaks(" in qml
    assert 'recipients.map(root.htmlEscape).join("\\n")' in qml


def test_qt_messages_toolbar_toggles_dismissible_settings_pane() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert 'text: qsTr("Settings")' in qml
    assert "onTriggered: root.togglePhoneSettings()" in qml
    assert 'text: qsTr("Close Settings")' in qml
    assert "pageStack.removePage(page)" in qml
    assert 'text: qsTr("Refresh")' not in qml


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
    assert '"contacts-json"' in quickshell


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


def test_all_gui_clients_explain_phone_side_pairing_step() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = _qml_bundle(ROOT / "data/quickshell")

    for client in (gtk, qt, quickshell):
        assert "find this computer" in client
        assert "Other Devices" in client
        assert "tap it" in client


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


def test_gui_clients_open_encrypted_storage_without_setup_buttons() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt_controller = (ROOT / "src/blueferry/qt/controller.py").read_text()
    qt_qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "_unlock_storage_button" not in gtk
    assert "_maybe_unlock_storage" in qt_controller
    assert "onClicked: root.bridge.unlockStorage()" not in qt_qml
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
