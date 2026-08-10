"""Installed identities agree with the public runtime protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from blueferry.protocol import BUS_NAME, OBJECT_PATH

ROOT = Path(__file__).resolve().parents[1]


def test_graphical_commands_follow_client_package_names() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text()
    desktop = (ROOT / "data" / "io.weirdware.BlueFerry.Gtk.desktop").read_text()

    assert 'blueferry-gtk = "blueferry.ui.app:main"' in project
    assert "/usr/bin/blueferry-gtk" in pkgbuild
    assert "Exec=blueferry-gtk" in desktop
    assert "blueferry-ui" not in project + pkgbuild + desktop


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
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

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
    assert "QtWidgets" not in (ROOT / "src" / "blueferry" / "qt" / "app.py").read_text()


def test_qt_messages_toolbar_toggles_dismissible_settings_pane() -> None:
    qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()

    assert 'text: qsTr("Settings")' in qml
    assert "onTriggered: root.togglePhoneSettings()" in qml
    assert 'text: qsTr("Close Settings")' in qml
    assert "pageStack.removePage(page)" in qml
    assert 'text: qsTr("Refresh")' not in qml


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
    qt_qml = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert 'tooltip_text=_("New Message")' in gtk
    assert "find_contacts_async" in gtk
    assert "def findContacts" in qt_controller
    assert 'text: qsTr("New Message")' in qt_qml
    assert 'Accessible.name: "New message"' in quickshell
    assert '"contacts-json"' in quickshell


def test_all_gui_clients_explain_phone_side_pairing_step() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    for client in (gtk, qt, quickshell):
        assert "When this computer shows up" in client
        assert "Other Devices" in client
        assert "tap it" in client


def test_all_gui_clients_explain_map_connection_refusal() -> None:
    message = (
        "iPhone is refusing message connections; is it connected to another computer?"
    )
    gtk_messages = (ROOT / "src/blueferry/ui/conversations.py").read_text()
    gtk_settings = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "map_connection_refused_message" in gtk_messages
    assert "map_connection_refused_message" in gtk_settings
    assert qt.count(message) >= 2
    assert quickshell.count(message) >= 2


def test_all_gui_clients_offer_unencrypted_local_storage() -> None:
    gtk = (ROOT / "src/blueferry/ui/status.py").read_text()
    qt = (ROOT / "src/blueferry/qt/qml/Main.qml").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

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

    assert "install -Dm644 data/quickshell/Theme.qml" in pkgbuild
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
    assert "component FerryButton: Button" in shell
    assert "id: bubble" in shell
    assert "messageTimestamp.implicitWidth" in shell
    assert "theme.primarySurface" in shell
    assert 'text: "BLUEFERRY"' not in shell
    assert shell.count('text: "⚙"') == 2
    assert "labelSize: theme.displaySize" in shell
    assert "bare: true" in shell
    assert "if (sendMessageButton.enabled) sendMessageButton.clicked()" in shell
    assert "id: settingsDeck" in shell
    assert 'text: "← MESSAGES"' not in shell
    assert "Choose which iPhone events create desktop popups" not in shell
    assert 'if (root.configured) return "• " + tasks' in shell
    assert "I will also forget this computer" not in shell
    assert "text: root.storageDetail" not in shell
    assert "Cannot retrieve or send messages - are you connected to another computer?" in shell
    assert "? root.mapConnectionRefused()" in shell
    assert 'text: root.phoneSettingsVisible ? "MESSAGES" : "IPHONE"' not in shell
    assert "component FerrySectionLabel: FerryLabel" in shell
    window_declaration = shell.split("FloatingWindow {", 1)[1].split("Pane {", 1)[0]
    assert 'color: "transparent"' not in window_declaration
