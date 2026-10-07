"""po/POTFILES.in must list every source with translatable strings.

A file missing from the inventory silently drops its strings from any
future translation template, so this is enforced instead of remembered.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POTFILES = ROOT / "po" / "POTFILES.in"
SRC = ROOT / "src"
# Defines the gettext helpers; it contains no messages of its own.
I18N_MODULE_NAME = "blueferry.i18n"
# The blueferry.i18n names that translate a string. Its other names, such as
# DOMAIN and LOCALE_DIR, are configuration and do not make a module translatable.
TRANSLATION_HELPERS = frozenset({"_", "ngettext"})
# The translation functions QML provides globally. lupdate extracts exactly
# these; anything else, such as a made-up qsTrNoOp, is not a marker.
_QML_MARKER_RE = re.compile(
    r"\b(?:qsTr|qsTranslate|qsTrId|QT_TR_NOOP|QT_TRANSLATE_NOOP|QT_TRID_NOOP)\s*\("
)
# A string literal or a comment, whichever starts first. Matching both in one
# pass keeps "//" inside a URL string from being taken for a comment, and a
# marker mentioned inside a string from counting as a call.
_QML_STRING_OR_COMMENT_RE = re.compile(
    r"""
    (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    """,
    re.DOTALL | re.VERBOSE,
)


def _listed() -> list[str]:
    return [
        line.strip()
        for line in POTFILES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_base(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    """Resolve the module an ``import from`` names, including relative forms."""
    if node.level == 0:
        return node.module
    package = module.split(".") if is_package else module.split(".")[:-1]
    if node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def _helper_names(
    tree: ast.Module,
    module: str,
    is_package: bool,
    providers: dict[str, frozenset[str]],
) -> tuple[frozenset[str], bool]:
    """Names this module binds to translation helpers, and whether it uses any.

    A helper is reached by importing it from a provider (blueferry.i18n or a
    module that re-exports a helper), possibly relatively or under an alias,
    or as an attribute of an imported provider module. Importing anything
    else, such as DOMAIN or LOCALE_DIR, does not count.
    """
    bound: set[str] = set()
    module_aliases: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _import_base(node, module, is_package)
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*" and base in providers:
                    bound |= providers[base]
                elif alias.name in providers.get(base, ()):
                    bound.add(alias.asname or alias.name)
                elif f"{base}.{alias.name}" in providers:
                    module_aliases[alias.asname or alias.name] = providers[f"{base}.{alias.name}"]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in providers:
                    module_aliases[alias.asname or alias.name] = providers[alias.name]
    used_through_module = any(
        isinstance(node, ast.Attribute)
        and node.attr in module_aliases.get(_dotted(node.value) or "", ())
        for node in ast.walk(tree)
    )
    return frozenset(bound), used_through_module


def _translatable_python(src_root: Path) -> set[Path]:
    """Python modules under src_root that use a translation helper."""
    modules = {
        path: (
            _module_name(path, src_root),
            path.name == "__init__.py",
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in src_root.rglob("*.py")
    }
    providers: dict[str, frozenset[str]] = {I18N_MODULE_NAME: TRANSLATION_HELPERS}
    translatable: set[Path] = set()
    changed = True
    while changed:  # re-exports can chain, so repeat until nothing new appears
        changed = False
        for path, (module, is_package, tree) in modules.items():
            if module == I18N_MODULE_NAME:
                continue
            names, used_through_module = _helper_names(tree, module, is_package, providers)
            if not names and not used_through_module:
                continue
            translatable.add(path)
            exported = providers.get(module, frozenset()) | names
            if exported != providers.get(module):
                providers[module] = exported
                changed = True
    return translatable


def _qml_code(text: str) -> str:
    """Return QML source with comments and string contents blanked out."""
    return _QML_STRING_OR_COMMENT_RE.sub(
        lambda match: '""' if match.group("string") else " ",
        text,
    )


def _qml_translatable(path: Path) -> bool:
    """True when a QML file calls one of Qt's translation functions."""
    return bool(_QML_MARKER_RE.search(_qml_code(path.read_text(encoding="utf-8"))))


def _translatable_sources() -> set[str]:
    python = {path.relative_to(ROOT).as_posix() for path in _translatable_python(SRC)}
    qml = {
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "data")
        for path in base.rglob("*.qml")
        if _qml_translatable(path)
    }
    return python | qml


def test_every_translatable_source_is_listed_in_potfiles() -> None:
    missing = sorted(_translatable_sources() - set(_listed()))

    assert missing == [], f"add to po/POTFILES.in: {missing}"


def test_potfiles_entries_exist_and_are_unique() -> None:
    listed = _listed()

    assert len(listed) == len(set(listed)), "po/POTFILES.in has duplicate entries"
    assert [entry for entry in listed if not (ROOT / entry).is_file()] == []


def test_translation_helpers_exist_in_i18n() -> None:
    from blueferry import i18n

    assert all(callable(getattr(i18n, name, None)) for name in TRANSLATION_HELPERS)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"blueferry/a.py": "from blueferry.i18n import _\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "from blueferry.i18n import ngettext\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "from blueferry.i18n import _ as tr\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "from blueferry.i18n import *\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "from . import i18n\ni18n._('x')\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "from blueferry import i18n\ni18n._('x')\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "import blueferry.i18n\nblueferry.i18n._('x')\n"}, {"blueferry/a.py"}),
        ({"blueferry/a.py": "import blueferry.i18n as t\nt.ngettext('a', 'b', 2)\n"}, {"blueferry/a.py"}),
        ({"blueferry/ui/a.py": "from ..i18n import _\n"}, {"blueferry/ui/a.py"}),
        ({"blueferry/ui/__init__.py": "from ..i18n import _\n"}, {"blueferry/ui/__init__.py"}),
        ({"blueferry/ui/__init__.py": "from .. import i18n\ni18n._('x')\n"}, {"blueferry/ui/__init__.py"}),
        (
            {
                "blueferry/a.py": "from blueferry.i18n import _\n",
                "blueferry/ui/b.py": "from ..a import _\n",
                "blueferry/ui/c.py": "from blueferry.ui.b import _ as tr\n",
                "blueferry/ui/d.py": "from .c import tr\n",
            },
            {"blueferry/a.py", "blueferry/ui/b.py", "blueferry/ui/c.py", "blueferry/ui/d.py"},
        ),
        ({"blueferry/a.py": "from blueferry.i18n import DOMAIN, LOCALE_DIR\n"}, set()),
        ({"blueferry/a.py": "from blueferry import i18n\nprint(i18n.DOMAIN)\n"}, set()),
        ({"blueferry/a.py": "from blueferry import config\n"}, set()),
        ({"blueferry/a.py": "_ = str\n"}, set()),
        ({"blueferry/a.py": "from .config import _\n"}, set()),
    ],
)
def test_gettext_detection(tmp_path, files: dict[str, str], expected: set[str]) -> None:
    sources = {"blueferry/__init__.py": "", "blueferry/i18n.py": "_ = str\n", **files}
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    (tmp_path / "blueferry" / "ui").mkdir(exist_ok=True)
    (tmp_path / "blueferry" / "ui" / "__init__.py").touch()

    found = {path.relative_to(tmp_path).as_posix() for path in _translatable_python(tmp_path)}

    assert found == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('text: qsTr("Send")\n', True),
        ('text: qsTranslate("Main", "Send")\n', True),
        ('text: qsTrId("send-button")\n', True),
        ('property var labels: [QT_TR_NOOP("Send")]\n', True),
        ('property var labels: [QT_TRANSLATE_NOOP("Main", "Send")]\n', True),
        ('property var labels: [QT_TRID_NOOP("send-button")]\n', True),
        ('text: qsTrNoOp("Send")\n', False),
        ('text: "Send"\n', False),
        ('// text: qsTr("Send")\ntext: "Send"\n', False),
        ('/* text: qsTr("Send")\n */\ntext: "Send"\n', False),
        ('text: "see qsTr(docs)"\n', False),
        ('source: "https://example.invalid"; text: qsTr("Open")\n', True),
    ],
)
def test_qml_marker_detection(tmp_path, source: str, expected: bool) -> None:
    path = tmp_path / "Sample.qml"
    path.write_text(source, encoding="utf-8")

    assert _qml_translatable(path) is expected
