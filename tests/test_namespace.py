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

    assert '"blueferry.qt" = ["qml/*.qml"]' in project
    assert "'kirigami'" in pkgbuild
    assert "'qqc2-desktop-style'" in pkgbuild
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


def test_quickshell_launcher_can_focus_an_existing_conversation() -> None:
    launcher = (ROOT / "data" / "blueferry-quickshell").read_text()
    qml = (ROOT / "data" / "quickshell" / "shell.qml").read_text()

    assert "--thread" in launcher
    assert "--message" in launcher
    assert "ipc call blueferry" in launcher
    assert 'target: "blueferry"' in qml
    assert "function openThread(key: string)" in qml
    assert "function openMessage(handle: string)" in qml
    assert "BLUEFERRY_OPEN_THREAD_KEY" in qml
    assert "BLUEFERRY_OPEN_MESSAGE_HANDLE" in qml


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


def test_quickshell_package_ships_shared_qml_without_the_qt_client() -> None:
    pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()
    assert "install -Dm644 data/quickshell/*.qml" in pkgbuild
    assert "install -Dm644 src/blueferry/qt/qml/ConversationLogic.qml" in pkgbuild


def test_remote_qml_text_is_rendered_as_plain_text() -> None:
    qt_qml = _qml_bundle(ROOT / "src/blueferry/qt/qml")
    quickshell = (ROOT / "data/quickshell/shell.qml").read_text()

    assert (
        "text: contactDelegate.text\n"
        "textFormat: Text.PlainText"
    ) in "\n".join(line.strip() for line in qt_qml.splitlines())
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
