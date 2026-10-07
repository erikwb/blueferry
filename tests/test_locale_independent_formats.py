"""Keep locale-dependent date formats out of the shipped code.

Every client surrounds its timestamps with English words ("Today", "at"), but
GTK and Qt call setlocale(LC_ALL, "") at startup. From then on strftime's name
directives (%a, %A, %b, %B, %p, %c, %x, %X, ...) follow LC_TIME, and so do the
QML/JavaScript locale formatters. time_display.py spells the names out instead;
this check stops new code from bringing the leak back.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "blueferry"

# Name, AM/PM and preferred-representation directives, with optional flags and
# the E/O alternative-representation modifiers, which are locale-defined too.
_LOCALE_DIRECTIVE = re.compile(r"%[-_0^#]?(?:[EO][A-Za-z]|[aAbBhpPcxXr])")
_FORMAT_CALLS = frozenset({"strftime", "strptime"})
_QML_LOCALE_FORMATTERS = re.compile(
    r"\b(?:toLocale(?:Date|Time)?String|Qt\.format(?:Date|Time|DateTime))\s*\("
)


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _format_strings(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Yield every string literal used as a date format in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _FORMAT_CALLS:
                candidates = node.args[:2]
            elif name == "format" and isinstance(func, ast.Name):
                candidates = node.args[1:2]
            else:
                continue
            for arg in candidates:
                text = _literal_text(arg)
                if text is not None:
                    yield node.lineno, text
        elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            for part in ast.walk(node.format_spec):
                text = _literal_text(part)
                if text is not None:
                    yield node.lineno, text


def _python_findings(source: str, path: str = "<snippet>") -> list[str]:
    return [
        f"{path}:{line}: {text!r}"
        for line, text in _format_strings(ast.parse(source))
        if _LOCALE_DIRECTIVE.search(text.replace("%%", ""))
    ]


def _qml_findings(source: str, path: str = "<snippet>") -> list[str]:
    return [
        f"{path}:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if _QML_LOCALE_FORMATTERS.search(line.split("//", 1)[0])
    ]


@pytest.mark.parametrize(
    "snippet",
    [
        'now.strftime("%A at %H:%M")',
        'time.strftime("%b %d")',
        'datetime.strptime(raw, "%d %B %Y")',
        'f"{stamp:%I:%M %p}"',
        'format(stamp, "%c")',
        'now.strftime("%-d %Ec")',
        'now.strftime("%x")',
    ],
)
def test_detector_flags_locale_dependent_formats(snippet: str) -> None:
    assert _python_findings(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        'now.strftime("%Y%m%dT%H%M%SZ")',
        'datetime.strptime(ts, "%Y%m%dT%H%M%S%z")',
        'f"{stamp:%H:%M}"',
        'f"{count:5d}"',
        '"%(asctime)s %(message)s" % record',
        'now.strftime("100%% at %H")',
        'now.strftime("%%a literally")',
    ],
)
def test_detector_accepts_numeric_formats(snippet: str) -> None:
    assert not _python_findings(snippet)


def test_detector_flags_qml_locale_formatters() -> None:
    assert _qml_findings("text: stamp.toLocaleTimeString()")
    assert _qml_findings("text: Qt.formatDateTime(stamp, 'ddd')")
    assert not _qml_findings("// Qt.formatDateTime(stamp) is locale-dependent")


def test_shipped_code_uses_no_locale_dependent_date_formats() -> None:
    findings: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(SOURCE_ROOT))
        findings += _python_findings(path.read_text(encoding="utf-8"), relative)
    for pattern in ("*.qml", "*.js"):
        for path in sorted(SOURCE_ROOT.rglob(pattern)):
            relative = str(path.relative_to(SOURCE_ROOT))
            findings += _qml_findings(path.read_text(encoding="utf-8"), relative)

    assert not findings, (
        "Locale-dependent date formats follow LC_TIME once GTK or Qt calls "
        "setlocale(); use blueferry.time_display instead:\n" + "\n".join(findings)
    )
