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
    assert "pageStack.layers.push(iphonePageComponent)" in qml
    assert "Kirigami.AboutPage" in qml
    assert "customFooterActions" in qml
    assert "interval: 3000" not in qml
    assert "QtWidgets" not in (ROOT / "src" / "blueferry" / "qt" / "app.py").read_text()


def test_gui_clients_keep_phone_settings_out_of_primary_navigation() -> None:
    gtk_window = (ROOT / "src/blueferry/ui/window.py").read_text()
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert "Adw.ViewSwitcher" not in gtk_window
    assert 'menu.append(_("iPhone Settings"), "win.phone")' in gtk_window
    assert "display: AbstractButton.IconOnly" in quickshell
    assert 'text: "Messages"' not in quickshell
    assert 'text: "iPhone"' not in quickshell


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
    assert "color: theme.windowSurface" in shell
    window_declaration = shell.split("FloatingWindow {", 1)[1].split("Pane {", 1)[0]
    assert 'color: "transparent"' not in window_declaration
