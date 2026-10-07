"""The conftest GLib source guard sees every real timer a test can arm."""
from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from gi.repository import GLib

_ROOT = Path(__file__).resolve().parent.parent
_GLIB_SCHEDULERS = {"timeout_add", "timeout_add_seconds", "idle_add", "source_remove"}


def _glib_default_seams() -> list[tuple[str, str, str, str]]:
    """(module, qualified callable, parameter, GLib name) for every default.

    Found by scanning the source instead of listing classes by hand, so a new
    supervisor with a GLib default is covered without touching this file.
    """
    seams = []
    for path in sorted((_ROOT / "src" / "blueferry").rglob("*.py")):
        module = ".".join(path.relative_to(_ROOT / "src").with_suffix("").parts)
        module = module.removesuffix(".__init__")

        def visit(node, prefix, module=module):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, [*prefix, child.name])
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = child.args
                    positional = args.posonlyargs + args.args
                    pairs = list(zip(
                        positional[len(positional) - len(args.defaults):], args.defaults,
                        strict=True,
                    )) + [
                        (arg, default)
                        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                        if default is not None
                    ]
                    for arg, default in pairs:
                        if (
                            isinstance(default, ast.Attribute)
                            and isinstance(default.value, ast.Name)
                            and default.value.id == "GLib"
                            and default.attr in _GLIB_SCHEDULERS
                        ):
                            seams.append((
                                module, ".".join([*prefix, child.name]),
                                arg.arg, default.attr,
                            ))

        visit(ast.parse(path.read_text()), [])
    return seams


_SEAMS = _glib_default_seams()


def test_the_source_scan_finds_the_scheduler_seams() -> None:
    assert any(seam[1] == "AncsClient.__init__" for seam in _SEAMS)


@pytest.mark.parametrize(
    ("module", "qualname", "parameter", "wrapped_name"), _SEAMS,
    ids=[f"{seam[1]}-{seam[2]}" for seam in _SEAMS],
)
def test_import_time_scheduler_defaults_are_recorded(
    module, qualname, parameter, wrapped_name,
) -> None:
    # These defaults are bound when the module is imported. The guard only
    # covers them because conftest wraps GLib before blueferry is imported.
    owner = importlib.import_module(module)
    for part in qualname.split("."):
        owner = getattr(owner, part)
    default = inspect.signature(owner).parameters[parameter].default
    assert default is getattr(GLib, wrapped_name)
    assert inspect.unwrap(default) is not default


def test_tests_that_fake_one_scheduler_seam_fake_all_of_them() -> None:
    # A fake schedule next to the real GLib.source_remove cancels made-up ids
    # on the default context; a fake cancel next to a real schedule leaks.
    seams: dict[str, set[str]] = {}
    for _module, qualname, parameter, _name in _SEAMS:
        owner, _, method = qualname.rpartition(".")
        if method == "__init__" and owner:
            seams.setdefault(owner.rpartition(".")[2], set()).add(parameter)
    partial = []
    for path in sorted((_ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in seams:
                continue
            passed = {keyword.arg for keyword in node.keywords}
            if None in passed:
                continue  # **kwargs: cannot tell statically
            injected = passed & seams[name]
            if injected and injected != seams[name]:
                missing = sorted(seams[name] - injected)
                partial.append(f"{path.name}:{node.lineno} {name} lacks {missing}")
    assert partial == []


def test_guard_reports_a_live_timer_until_it_is_removed(glib_source_guard) -> None:
    source_id = GLib.timeout_add_seconds(3600, lambda: False)
    assert [armed[0] for armed in glib_source_guard.live()] == [source_id]
    GLib.source_remove(source_id)
    assert glib_source_guard.live() == []


def test_guard_forgets_an_idle_source_that_already_ran(glib_source_guard) -> None:
    ran = []
    GLib.idle_add(lambda: ran.append(True) or False)
    context = GLib.MainContext.default()
    while not ran:
        context.iteration(True)
    assert glib_source_guard.live() == []


def test_guard_ignores_sources_armed_from_worker_threads(glib_source_guard) -> None:
    # Workers post idle callbacks back to the main loop whenever they finish,
    # which may be during a later test. Only the test's own thread counts.
    armed = []
    worker = threading.Thread(target=lambda: armed.append(GLib.idle_add(lambda: False)))
    worker.start()
    worker.join()
    try:
        assert glib_source_guard.armed == []
        assert glib_source_guard.live() == []
    finally:
        GLib.source_remove(armed[0])
    assert glib_source_guard.foreign_removals == [armed[0]]
    glib_source_guard.foreign_removals.clear()


# Appended to a copy of conftest.py: an autouse fixture that is set up before
# the guard, so its finalizer runs after the guard fixture's would.
_LATE_CLEANUP = '''

@pytest.fixture(autouse=True)
def aaa_late_cleanup(request):
    assert _glib_guard_thread is None, "fixture must be set up before the guard"
    yield
    for source_id in getattr(request.node, "late_sources", ()):
        GLib.source_remove(source_id)
'''

_INNER_TESTS = '''
import pytest
from gi.repository import GLib


def test_leak():
    GLib.timeout_add_seconds(3600, lambda: False)


def test_leak_after_failure():
    GLib.timeout_add_seconds(3600, lambda: False)
    raise RuntimeError("body failed before its cleanup")


def test_late_fixture_cleanup(request):
    request.node.late_sources = [GLib.timeout_add_seconds(3600, lambda: False)]


def test_fake_id_removed():
    GLib.source_remove(987654)


def test_clean():
    GLib.source_remove(GLib.timeout_add_seconds(3600, lambda: False))
'''


def test_guard_enforces_at_teardown_without_double_reports(tmp_path) -> None:
    conftest = Path(__file__).with_name("conftest.py").read_text()
    (tmp_path / "conftest.py").write_text(conftest + _LATE_CLEANUP)
    (tmp_path / "test_inner.py").write_text(_INNER_TESTS)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA",
         "-W", "ignore", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )
    lines = result.stdout.splitlines()

    def outcome(name):
        return sorted(
            line.split()[0] for line in lines
            if line.endswith(f"test_inner.py::{name}")
            or f"test_inner.py::{name} - " in line
        )

    assert outcome("test_leak") == ["ERROR", "PASSED"], result.stdout
    assert "test left GLib sources armed" in result.stdout
    # The body failure is the report; the leaked timer is only removed.
    assert outcome("test_leak_after_failure") == ["FAILED"], result.stdout
    # A finalizer that runs after the guard fixture still counts as cleanup.
    assert outcome("test_late_fixture_cleanup") == ["PASSED"], result.stdout
    assert outcome("test_fake_id_removed") == ["ERROR", "PASSED"], result.stdout
    assert "[987654]" in result.stdout
    assert outcome("test_clean") == ["PASSED"], result.stdout
