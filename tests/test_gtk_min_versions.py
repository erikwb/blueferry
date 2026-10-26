"""The GTK client runs on libadwaita 1.5 and GTK 4.14 (Ubuntu 24.04).

Symbols added later (AdwButtonRow, AdwSpinner, AdwToggleGroup,
AdwPreferencesPage:banner, ...) fail there at run time, so the GTK sources
must not use them without a fallback. The check reads the "version"
attribute that the GIR files carry for every symbol added after 1.0/4.0. A
qualified enum member (``Gtk.Type.MEMBER``) is checked against its type; a
method, property or signal name, whose class the scan cannot tell, counts
as newer when any Adw or GTK type gained it later. Without the GIR
files, an explicit list of the newer libadwaita symbols is checked instead.
"""
from __future__ import annotations

import os
import re
import sys
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


def _newer_symbols() -> tuple[dict[str, set[str]], set[tuple[str, str, str]]]:
    """``({namespace: newer top-level names}, {(namespace, type, member)})``
    for members added after the minimum to a type that is old enough."""
    newer_top: dict[str, set[str]] = {}
    newer_members: set[tuple[str, str, str]] = set()
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
                        newer_members.add((namespace, name, member_name))
    return newer_top, newer_members


# Method names of Python's own containers (``text.find``, ``rows.insert``)
# would match GTK methods of the same name added later. The price: such a
# name is not checked on an Adw/Gtk receiver either (Adw.PreferencesPage
# .insert is libadwaita 1.8, Gtk.StringList.find GTK 4.16); the scan cannot
# tell the receiver's type.
_PYTHON_METHODS = {
    name for kind in (str, bytes, list, dict, set, tuple) for name in dir(kind)
    if not name.startswith("_")
}
_CONSTRUCTOR = re.compile(r"\b(Adw|Gtk)\.([A-Z]\w*)\(")


def _constructor_calls(source: str) -> list[tuple[str, str, str]]:
    """``(namespace, class, top-level arguments)`` of each Adw/Gtk constructor
    call. Nested calls (``title=_("x")``) and string literals are blanked, so
    only the call's own keywords remain; the nested calls are found on their
    own."""
    calls = []
    for match in _CONSTRUCTOR.finditer(source):
        depth, quote, top = 1, "", []
        index = match.end()
        while index < len(source) and depth:
            char = source[index]
            if quote:
                if char == "\\":
                    index += 1
                elif char == quote:
                    quote = ""
                char = " "
            elif char in "'\"":
                quote, char = char, " "
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if depth == 1 and char not in "([{)]}":
                top.append(char)
            index += 1
        calls.append((match[1], match[2], "".join(top)))
    return calls


def _uses(source: str) -> tuple[set[tuple[str, str]], set[tuple[str, str, str]], set[str]]:
    """``(Adw/Gtk top-level names, qualified Type.MEMBER uses, other member names)``.

    A qualified ``Gtk.Type.MEMBER`` is checked against that type only. For a
    method call, keyword argument, property or signal name the scan cannot
    tell the class, so the name counts as newer if any type gained it later.
    """
    top = set(re.findall(r"\b(Adw|Gtk)\.([A-Za-z_]\w*)", source))
    qualified = set(re.findall(r"\b(Adw|Gtk)\.(\w+)\.([A-Z][A-Z0-9_]*)\b", source))
    members = set(re.findall(r"\.([A-Za-z_]\w*)\s*\(", source)) - _PYTHON_METHODS  # calls
    members |= {name.replace("-", "_") for name in re.findall(
        r"""["'](?:notify::)?([a-z][a-z0-9-]*)["']""", source)}  # properties, signals
    # Keyword arguments only inside Adw/Gtk constructor calls, not every assignment.
    for _namespace, _name, arguments in _constructor_calls(source):
        members |= set(re.findall(r"\b([a-z][a-z0-9_]*)\s*=(?!=)", arguments))
    return top, qualified, members


def _found(newer_top, newer_members) -> list[str]:
    newer_names = {name for _ns, kind, name in newer_members if not name.isupper()}
    found = []
    for path in SOURCES:
        top, qualified, members = _uses(path.read_text())
        found += [f"{path.name}: {ns}.{name}" for ns, name in sorted(top)
                  if name in newer_top[ns]]
        found += [f"{path.name}: {ns}.{kind}.{name}" for ns, kind, name in sorted(qualified)
                  if (ns, kind, name) in newer_members]
        found += [f"{path.name}: {name}" for name in sorted(members & newer_names)]
    return found


def test_gtk_sources_use_no_symbols_newer_than_the_minimum() -> None:
    found = _found(*_newer_symbols())
    assert found == [], "libadwaita > 1.5 / GTK > 4.14 symbols: " + ", ".join(found)


def test_gtk_sources_avoid_the_known_newer_libadwaita_symbols() -> None:
    # Also holds where the GIR files are not installed.
    found = []
    for path in SOURCES:
        top, _qualified, members = _uses(path.read_text())
        found += [f"{path.name}: Adw.{name}" for ns, name in sorted(top)
                  if ns == "Adw" and name in FALLBACK_ADW]
        found += [f"{path.name}: {name}" for name in sorted(members & FALLBACK_MEMBERS)]
    assert found == []


def test_the_check_sees_newer_symbols(monkeypatch, tmp_path) -> None:
    newer_top, newer_members = _newer_symbols()
    assert {"ButtonRow", "Spinner", "ToggleGroup", "WrapBox", "BottomSheet"} <= newer_top["Adw"]
    assert "PreferencesDialog" not in newer_top["Adw"]
    assert ("Adw", "PreferencesPage", "set_banner") in newer_members
    sample = tmp_path / "sample.py"
    sample.write_text(
        "row = Adw.ButtonRow(title='x')\npage.set_banner(banner)\n"
        "Adw.PreferencesPage(description_centered=True)\n"
        "icon = theme.lookup_icon('x', None, 16, 1, 0, Gtk.IconLookupFlags.NONE)\n"
        "box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)\n"
        "row = Adw.EntryRow(title=_('Number (mobile)'), max_length=64)\n"
        "page = Adw.PreferencesPage(\n    title=_('x'), banner_text='a=b',\n    description_centered=True,\n)\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "SOURCES", [sample])
    found = _found(newer_top, newer_members)
    assert "sample.py: Adw.ButtonRow" in found
    assert {"sample.py: set_banner", "sample.py: description_centered"} <= set(found)
    # Keywords after a nested call such as _("…") count too.
    assert "sample.py: max_length" in found
    [(_ns, _name, arguments)] = [
        call for call in _constructor_calls("Adw.PreferencesPage(title=_('x'), text='a=b')")
        if call[1] == "PreferencesPage"]
    assert re.findall(r"\b([a-z][a-z0-9_]*)\s*=(?!=)", arguments) == ["title", "text"]
    # Enum members count per type: IconLookupFlags.NONE is GTK 4.18,
    # SelectionMode.NONE is old.
    assert "sample.py: Gtk.IconLookupFlags.NONE" in found
    assert not any("SelectionMode" in entry for entry in found)
