"""The Qt and GTK desktop entries offer BlueFerry for phone links."""
from __future__ import annotations

import configparser
import shutil
import subprocess  # nosec B404
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("name", "command"), [
    ("io.weirdware.BlueFerry.Qt.desktop", "blueferry-qt"),
    ("io.weirdware.BlueFerry.Gtk.desktop", "blueferry-gtk"),
])
def test_desktop_entries_handle_phone_links(name, command):
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str  # type: ignore[assignment,method-assign]
    entry.read(ROOT / "data" / name, encoding="utf-8")
    desktop = entry["Desktop Entry"]
    # %u expands to nothing for a plain launch, which is unchanged.
    assert desktop["Exec"] == f"{command} %u"
    assert desktop["MimeType"] == "x-scheme-handler/tel;x-scheme-handler/callto;"


@pytest.mark.parametrize("name", [
    "io.weirdware.BlueFerry.Qt.desktop", "io.weirdware.BlueFerry.Gtk.desktop",
])
def test_desktop_entries_pass_desktop_file_validate(name):
    validate = shutil.which("desktop-file-validate")
    if validate is None:
        pytest.skip("desktop-file-validate is not installed")
    # Fixed tool and a repository path; no shell.
    subprocess.run(  # nosec B603
        [validate, str(ROOT / "data" / name)], check=True, capture_output=True, timeout=30,
    )
