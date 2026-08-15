from __future__ import annotations

import pathlib

import tomllib

ROOT = pathlib.Path(__file__).parents[1]


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def test_native_recipes_track_project_version() -> None:
    version = _version()
    debian_changelog = (ROOT / "packaging/deb/changelog").read_text()
    rpm_spec = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    assert debian_changelog.startswith(f"blueferry ({version}-1)")
    assert f"Version:        {version}\n" in rpm_spec


def test_native_packages_bake_and_ship_a_source_build_sha() -> None:
    scripts = [
        (ROOT / "build.sh").read_text(),
        (ROOT / "packaging/build-deb.sh").read_text(),
        (ROOT / "packaging/build-rpm.sh").read_text(),
    ]
    arch = (ROOT / "packaging/arch/PKGBUILD").read_text()
    deb_rules = (ROOT / "packaging/deb/rules").read_text()
    deb_install = (ROOT / "packaging/deb/blueferry-backend.install").read_text()
    rpm = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    for script in scripts:
        assert ".blueferry-build-sha" in script
        assert "sha256sum" in script
    assert "usr/share/blueferry/build-sha" in arch
    assert "usr/share/blueferry/build-sha" in deb_rules
    assert "usr/share/blueferry/build-sha" in deb_install
    assert "%{_datadir}/blueferry/build-sha" in rpm


def test_notification_capable_native_families_use_their_own_bluetoothd_path() -> None:
    arch = (ROOT / "packaging/arch/blueferry-bluetooth.conf").read_text()
    rpm = (ROOT / "packaging/rpm/blueferry-bluetooth.conf").read_text()

    assert "ExecStart=/usr/lib/bluetooth/bluetoothd -E" in arch
    assert "ExecStart=/usr/libexec/bluetooth/bluetoothd -E" in rpm
    assert not (ROOT / "packaging/deb/blueferry-bluetooth.conf").exists()


def test_notification_capable_packages_reload_and_restart_running_bluetooth() -> None:
    arch_recipe = (ROOT / "packaging/arch/PKGBUILD").read_text()
    arch_hook = (
        ROOT / "packaging/arch/70-blueferry-restart-bluetooth.hook"
    ).read_text()
    arch_install = (
        ROOT / "packaging/arch/blueferry-backend.install"
    ).read_text()
    rpm_spec = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    assert "'bluez>=5.86'" in arch_recipe
    assert "Requires:       bluez >= 5.86" in rpm_spec
    assert "70-blueferry-restart-bluetooth.hook" in arch_recipe
    assert "install=blueferry-backend.install" in arch_recipe
    assert "post_install()" in arch_install
    assert "post_upgrade()" in arch_install
    assert "systemd-hook daemon-reload-system" in arch_install
    assert arch_install.count("systemd-hook restart bluetooth.service") == 1
    upgrade = arch_install.split("post_upgrade()", maxsplit=1)[1]
    assert "daemon-reload-system" not in upgrade
    assert "_blueferry_reload_bluetooth_unit" in upgrade
    assert "restart bluetooth.service" not in upgrade
    for operation in ("Install", "Upgrade", "Remove"):
        assert f"Operation = {operation}" in arch_hook
    assert "systemd-hook restart bluetooth.service" in arch_hook

    assert rpm_spec.count("/usr/bin/systemctl daemon-reload") == 2
    assert rpm_spec.count("/usr/bin/systemctl try-restart bluetooth.service") == 2
    assert "if [ $1 -eq 0 ]" in rpm_spec


def test_deb_is_map_pbap_only_and_does_not_manage_bluetooth_service() -> None:
    control = (ROOT / "packaging/deb/control").read_text()
    rules = (ROOT / "packaging/deb/rules").read_text()
    install = (ROOT / "packaging/deb/blueferry-backend.install").read_text()
    readme = (ROOT / "packaging/deb/README.md").read_text()

    assert "bluez (>= 5.72)" in control
    assert "gir1.2-secret-1" in control
    assert "libsecret-1-0" in control
    assert "bluetooth.service" not in rules
    assert "bluetooth.service" not in install
    assert not (ROOT / "packaging/deb/blueferry-backend.postinst").exists()
    assert not (ROOT / "packaging/deb/blueferry-backend.postrm").exists()
    assert "MAP messages and PBAP contacts" in readme
    assert "never enables `-E` or" in readme
    assert "already has BlueZ 5.86 or newer" in readme


def test_deb_and_rpm_install_secret_service_client_bindings() -> None:
    control = (ROOT / "packaging/deb/control").read_text()
    spec = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    assert "gir1.2-secret-1" in control
    assert "libsecret-1-0" in control
    assert "Requires:       libsecret" in spec


def test_native_backends_install_systemd_privilege_dependencies() -> None:
    control = (ROOT / "packaging/deb/control").read_text()
    arch = (ROOT / "packaging/arch/PKGBUILD").read_text()
    spec = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    assert " polkitd,\n" in control
    assert " systemd,\n" in control
    assert "'polkit'" in arch
    assert "'systemd'" in arch
    assert "Requires:       polkit" in spec
    assert "Requires:       systemd" in spec
    assert "pkexec" not in control
    assert "pkexec" not in arch
    assert "pkexec" not in spec


def test_native_backends_ship_the_btmgmt_system_unit_template() -> None:
    unit_name = "blueferry-btmgmt-set-class@.service"
    unit = (ROOT / "systemd" / unit_name).read_text()
    deb_rules = (ROOT / "packaging/deb/rules").read_text()
    deb_install = (ROOT / "packaging/deb/blueferry-backend.install").read_text()
    arch = (ROOT / "packaging/arch/PKGBUILD").read_text()
    spec = (ROOT / "packaging/rpm/blueferry.spec").read_text()

    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/bin/btmgmt --index %i class 4 8" in unit
    assert "[Install]" not in unit
    assert f"systemd/{unit_name}" in deb_rules
    assert f"usr/lib/systemd/system/{unit_name}" in deb_install
    assert f"systemd/{unit_name}" in arch
    assert f"%{{_unitdir}}/{unit_name}" in spec


def test_deb_and_rpm_backend_ship_private_textual_runtime() -> None:
    control = (ROOT / "packaging/deb/control").read_text().lower()
    deb_install = (ROOT / "packaging/deb/blueferry-backend.install").read_text()
    spec = (ROOT / "packaging/rpm/blueferry.spec").read_text().lower()
    vendor = ROOT / "packaging/vendor/textual"

    assert "package: blueferry-tui" not in control
    assert "requires:       python3dist(textual)" not in spec
    assert "usr/bin/blueferry-tui" in deb_install
    assert "usr/lib/blueferry/vendor" in deb_install
    assert "%{_bindir}/blueferry-tui" in spec
    assert "%{_prefix}/lib/blueferry/vendor" in spec
    if vendor.is_dir():
        assert "textual-8.2.8-py3-none-any.whl" in (
            vendor / "SHA256SUMS"
        ).read_text()
        assert len(list((vendor / "wheels").glob("*.whl"))) == 10
    else:
        # Arch source snapshots deliberately omit the DEB/RPM-only bundle.
        assert not (ROOT / "packaging/vendor").exists()


def test_arch_snapshot_excludes_deb_rpm_only_vendor_bundle() -> None:
    build_script = (ROOT / "build.sh").read_text()
    arch_recipe = (ROOT / "packaging/arch/PKGBUILD").read_text()

    assert '[[ "$file" == packaging/vendor/textual/* ]]' in build_script
    assert "packaging/vendor/textual" not in arch_recipe
    assert build_script.index('rm -rf -- "$snapshot_work"') < build_script.index(
        'exec makepkg --cleanbuild --force "$@"'
    )


def test_package_workflow_installs_identical_artifacts_on_targets() -> None:
    workflow = (ROOT / ".github/workflows/packages.yml").read_text()

    for target in (
        "Debian 13",
        "Ubuntu 24.04",
        "Ubuntu 26.04",
        "PikaOS IV",
        "Linux Mint 22.3",
        "Pop!_OS 24.04",
        "Fedora 43",
        "Fedora 44",
    ):
        assert target in workflow
    assert "cachyos/cachyos:latest" in workflow
    assert "linuxmintd/mint22.3-amd64:latest" in workflow
    assert "http://apt.pop-os.org/release noble main" in workflow
    assert "name: packages-arch" in workflow
    assert "name: packages-deb" in workflow
    assert "name: packages-rpm" in workflow
    assert "Smoke-test the Arch backend and bundled TUI" in workflow
    assert "install the blueferry-tui package" not in workflow
    assert workflow.count("smoke_tui blueferry-tui") == 6
    assert workflow.count("smoke_tui blueferry tui") == 6
    assert workflow.count('gi.require_version("Secret", "1")') == 4
    assert workflow.count('safe.directory "$GITHUB_WORKSPACE"') == 2
    assert workflow.count("dnf install -y --allowerasing") == 2
    assert workflow.count(
        "systemd-analyze verify --man=no bluetooth.service"
    ) == 3
    assert "GH_REPO: ${{ github.repository }}" in workflow
