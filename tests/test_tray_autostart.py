"""The opt-in per-user autostart entry for blueferry-tray."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from blueferry import tray_autostart

ROOT = Path(__file__).resolve().parents[1]


def test_entry_follows_xdg_config_home_and_ignores_a_relative_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tray_autostart.entry_path({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "autostart" / "io.weirdware.BlueFerry.Tray.desktop"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert tray_autostart.entry_path({"XDG_CONFIG_HOME": "relative"}) == (
        tmp_path / "home" / ".config" / "autostart" / "io.weirdware.BlueFerry.Tray.desktop"
    )


def test_off_by_default_and_switches_on_and_off(tmp_path: Path) -> None:
    path = tmp_path / "autostart" / tray_autostart.ENTRY_NAME
    assert tray_autostart.is_enabled(path) is False

    tray_autostart.set_enabled(True, path)
    assert tray_autostart.is_enabled(path) is True
    assert path.read_text(encoding="utf-8") == tray_autostart.ENTRY_TEXT
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]

    tray_autostart.set_enabled(False, path)
    assert not path.exists()
    tray_autostart.set_enabled(False, path)  # already off: no error


@pytest.mark.parametrize("line", ["Hidden=true", "X-GNOME-Autostart-enabled=false"])
def test_an_entry_the_desktop_turned_off_counts_as_off(tmp_path: Path, line: str) -> None:
    path = tmp_path / tray_autostart.ENTRY_NAME
    path.write_text(f"[Desktop Entry]\nExec=blueferry-tray\n{line}\n", encoding="utf-8")
    assert tray_autostart.is_enabled(path) is False
    path.write_text(f"[Desktop Action x]\n{line}\n[Desktop Entry]\nExec=x\n", encoding="utf-8")
    assert tray_autostart.is_enabled(path) is True


def test_an_oversized_entry_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / tray_autostart.ENTRY_NAME
    path.write_text("#" * (tray_autostart.MAX_ENTRY_BYTES + 1), encoding="utf-8")
    assert tray_autostart.is_enabled(path) is False
