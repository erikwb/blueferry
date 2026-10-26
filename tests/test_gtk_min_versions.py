"""The GTK client runs on libadwaita 1.5 and GTK 4.14 (Ubuntu 24.04).

Symbols added later (AdwButtonRow, AdwSpinner, AdwToggleGroup,
AdwPreferencesPage:banner, ...) fail there at run time, so the GTK sources
must not use them without a fallback. The check reads the "version"
attribute that the GIR files carry for every symbol added after 1.0/4.0. A
member name is only flagged when no older Adw or GTK symbol has the same
name, since the scan cannot tell which class a call goes to. Without the GIR
files, an explicit list of the newer libadwaita symbols is checked instead.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET  # nosec B405 - local, trusted GIR files
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "src/blueferry/ui").glob("*.py"))
MINIMUM = {"Adw": (1, 5), "Gtk": (4, 14)}
_CORE = "{http://www.gtk.org/introspection/core/1.0}"
_TOP_LEVEL = ("class", "interface", "record", "enumeration", "bitfield", "function",
              "constant", "callback")
_MEMBERS = ("method", "constructor", "function", "property", "signal", "member")

# Used when the GIR files are missing: libadwaita symbols newer than 1.5.
FALLBACK_ADW = {
    "AccentColor", "BottomSheet", "ButtonRow", "InlineViewSwitcher", "Layout",
    "LayoutSlot", "MultiLayoutView", "ShortcutLabel", "ShortcutsDialog",
    "ShortcutsItem", "ShortcutsSection", "Spinner", "SpinnerPaintable", "Toggle",
    "ToggleGroup", "WrapBox", "WrapLayout", "BannerButtonStyle", "JustifyMode",
    "PackDirection", "WrapPolicy",
}
FALLBACK_MEMBERS = {"set_banner", "get_banner", "set_description_centered", "set_button_style"}


def _gir(namespace: str, version: str) -> Path | None:
    directories = [*os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep),
                   "/usr/local/share", "/usr/share"]
    for directory in filter(None, directories):
        path = Path(directory) / "gir-1.0" / f"{namespace}-{version}.gir"
        if path.is_file():
            return path
    return None


def _version(element) -> tuple[int, ...]:
    value = element.get("version") or "0"
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def _newer_symbols() -> tuple[dict[str, set[str]], set[str]]:
    """``({namespace: newer top-level names}, newer member names)``."""
    newer_top: dict[str, set[str]] = {}
    newer_members: set[str] = set()
    older_members: set[str] = set()
    for namespace, version in (("Adw", "1"), ("Gtk", "4.0")):
        path = _gir(namespace, version)
        if path is None:
            pytest.skip(f"{namespace}-{version}.gir is not installed")
        minimum = MINIMUM[namespace]
        root = ET.parse(path).getroot()  # nosec B314 - local, trusted GIR file
        tree = root.find(f"{_CORE}namespace")
        assert tree is not None
        newer_top[namespace] = set()
        for kind in _TOP_LEVEL:
            for element in tree.findall(f"{_CORE}{kind}"):
                name = element.get("name", "")
                if _version(element) > minimum:
                    newer_top[namespace].add(name)
                    continue
                for member in element:
                    if member.tag.removeprefix(_CORE) not in _MEMBERS:
                        continue
                    member_name = member.get("name", "").replace("-", "_")
                    if member.tag == f"{_CORE}member":
                        member_name = member_name.upper()
                    if _version(member) > minimum:
                        newer_members.add(member_name)
                    else:
                        older_members.add(member_name)
    return newer_top, newer_members - older_members


def _uses(source: str) -> tuple[set[tuple[str, str]], set[str]]:
    top = set(re.findall(r"\b(Adw|Gtk)\.([A-Za-z_]\w*)", source))
    members = set(re.findall(r"\.([A-Za-z_]\w*)\s*\(", source))  # calls
    members |= set(re.findall(r"\b(?:Adw|Gtk)\.\w+\.([A-Z][A-Z0-9_]*)\b", source))  # enums
    members |= {name.replace("-", "_") for name in re.findall(
        r"""["'](?:notify::)?([a-z][a-z0-9-]*)["']""", source)}  # properties, signals
    members |= set(re.findall(r"\b([a-z][a-z0-9_]*)\s*=(?!=)", source))  # keyword arguments
    return top, members


def test_gtk_sources_use_no_symbols_newer_than_the_minimum() -> None:
    newer_top, newer_members = _newer_symbols()
    found = []
    for path in SOURCES:
        top, members = _uses(path.read_text())
        found += [f"{path.name}: {ns}.{name}" for ns, name in sorted(top)
                  if name in newer_top[ns]]
        found += [f"{path.name}: {name}" for name in sorted(members & newer_members)]
    assert found == [], "libadwaita > 1.5 / GTK > 4.14 symbols: " + ", ".join(found)


def test_gtk_sources_avoid_the_known_newer_libadwaita_symbols() -> None:
    # Also holds where the GIR files are not installed.
    found = []
    for path in SOURCES:
        top, members = _uses(path.read_text())
        found += [f"{path.name}: Adw.{name}" for ns, name in sorted(top)
                  if ns == "Adw" and name in FALLBACK_ADW]
        found += [f"{path.name}: {name}" for name in sorted(members & FALLBACK_MEMBERS)]
    assert found == []


def test_the_check_sees_newer_symbols(tmp_path) -> None:
    newer_top, newer_members = _newer_symbols()
    assert {"ButtonRow", "Spinner", "ToggleGroup", "WrapBox", "BottomSheet"} <= newer_top["Adw"]
    assert "PreferencesDialog" not in newer_top["Adw"]
    top, members = _uses(
        "row = Adw.ButtonRow(title='x')\npage.set_banner(banner)\n"
        "Adw.PreferencesPage(description_centered=True)\n"
    )
    assert ("Adw", "ButtonRow") in top
    assert {"set_banner", "description_centered"} <= members & newer_members
