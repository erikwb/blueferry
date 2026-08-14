from __future__ import annotations

import builtins
import sys
from pathlib import Path

from typer.testing import CliRunner

from blueferry import cli, tui_launcher


def test_packaged_launcher_adds_vendor_in_process(monkeypatch, tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    monkeypatch.setattr(tui_launcher, "_PACKAGED_VENDOR", vendor)
    monkeypatch.setattr(tui_launcher, "_PACKAGED_PYTHON_ROOT", tmp_path)
    monkeypatch.setattr(tui_launcher, "_LAUNCHER_PATH", tmp_path / "blueferry/tui_launcher.py")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(vendor)])
    dropped = []
    monkeypatch.setattr(tui_launcher, "_drop_shadowed_modules", lambda: dropped.append(True))
    monkeypatch.setattr("blueferry.tui.main", lambda: 19)

    assert tui_launcher.main() == 19
    assert sys.path[0] == str(vendor)
    assert dropped == [True]


def test_checkout_does_not_use_an_installed_package_vendor(
    monkeypatch, tmp_path: Path,
) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    monkeypatch.setattr(tui_launcher, "_PACKAGED_VENDOR", vendor)
    monkeypatch.setattr(tui_launcher, "_PACKAGED_PYTHON_ROOT", tmp_path / "usr/lib")
    monkeypatch.setattr(tui_launcher, "_LAUNCHER_PATH", tmp_path / "checkout/tui_launcher.py")
    original = list(sys.path)
    monkeypatch.setattr("blueferry.tui.main", lambda: 23)

    assert tui_launcher.main() == 23
    assert sys.path == original


def test_launcher_does_not_duplicate_an_active_vendor_path(
    monkeypatch, tmp_path: Path,
) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    monkeypatch.setattr(tui_launcher, "_PACKAGED_VENDOR", vendor)
    monkeypatch.setattr(tui_launcher, "_PACKAGED_PYTHON_ROOT", tmp_path)
    monkeypatch.setattr(tui_launcher, "_LAUNCHER_PATH", tmp_path / "blueferry/tui_launcher.py")
    monkeypatch.setattr(sys, "path", [str(vendor), *sys.path])
    monkeypatch.setattr("blueferry.tui.main", lambda: 17)

    assert tui_launcher.main() == 17
    assert sys.path.count(str(vendor)) == 1


def test_launcher_drops_every_module_shadowed_by_the_vendor_bundle(monkeypatch) -> None:
    modules = {
        "blueferry": object(),
        "rich": object(),
        "rich.console": object(),
        "textual": object(),
        "textual.app": object(),
        "pygments": object(),
        "typing_extensions": object(),
        "typer": object(),
    }
    monkeypatch.setattr(sys, "modules", modules)

    tui_launcher._drop_shadowed_modules()

    assert set(modules) == {"blueferry", "typer"}


def test_missing_arch_tui_returns_an_install_hint(monkeypatch, capsys) -> None:
    real_import = builtins.__import__

    def missing_tui(name, *args, **kwargs):
        if name == "blueferry.tui":
            raise ModuleNotFoundError(
                "No module named 'blueferry.tui'",
                name="blueferry.tui",
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_tui)

    assert tui_launcher.main() == 2
    assert "install the blueferry-tui package" in capsys.readouterr().err


def test_backend_cli_reports_how_to_install_the_arch_tui(monkeypatch) -> None:
    real_import = builtins.__import__

    def missing_tui(name, *args, **kwargs):
        if name == "blueferry.tui":
            raise ModuleNotFoundError(
                "No module named 'blueferry.tui'",
                name="blueferry.tui",
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_tui)

    result = CliRunner().invoke(cli.app, ["tui"])

    assert result.exit_code == 2
    assert "install the blueferry-tui package" in result.output
