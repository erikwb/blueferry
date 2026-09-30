"""po/POTFILES.in must list every source with translatable strings.

A file missing from the inventory silently drops its strings from any
future translation template, so this is enforced instead of remembered.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POTFILES = ROOT / "po" / "POTFILES.in"
# Defines the gettext helpers; it contains no messages of its own.
I18N_MODULE = Path("src/blueferry/i18n.py")
_QSTR_RE = re.compile(r"\bqsTr(?:NoOp)?\s*\(")


def _listed() -> list[str]:
    return [
        line.strip()
        for line in POTFILES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _uses_gettext(path: Path) -> bool:
    """True when a module imports the shared blueferry.i18n helpers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "blueferry.i18n":
                return True
            if node.module == "blueferry" and any(alias.name == "i18n" for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name == "blueferry.i18n" for alias in node.names):
                return True
    return False


def _translatable_sources() -> set[str]:
    python = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "blueferry").rglob("*.py")
        if path.relative_to(ROOT) != I18N_MODULE and _uses_gettext(path)
    }
    qml = {
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "data")
        for path in base.rglob("*.qml")
        if _QSTR_RE.search(path.read_text(encoding="utf-8"))
    }
    return python | qml


def test_every_translatable_source_is_listed_in_potfiles() -> None:
    missing = sorted(_translatable_sources() - set(_listed()))

    assert missing == [], f"add to po/POTFILES.in: {missing}"


def test_potfiles_entries_exist_and_are_unique() -> None:
    listed = _listed()

    assert len(listed) == len(set(listed)), "po/POTFILES.in has duplicate entries"
    assert [entry for entry in listed if not (ROOT / entry).is_file()] == []


def test_gettext_detection_covers_import_forms(tmp_path) -> None:
    samples = {
        "from blueferry.i18n import _\n": True,
        "from blueferry.i18n import _, ngettext\n": True,
        "from blueferry import i18n\n": True,
        "import blueferry.i18n\n": True,
        "from blueferry import config\n": False,
        "_ = str\n": False,
    }
    for source, expected in samples.items():
        path = tmp_path / "sample.py"
        path.write_text(source, encoding="utf-8")
        assert _uses_gettext(path) is expected, source
