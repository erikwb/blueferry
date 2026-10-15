"""tools/lint_mainloop.py: the tree is clean and each rule fires."""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "tools" / "lint_mainloop.py"
lint_mainloop = sys.modules.get("_blueferry_lint_mainloop")
if lint_mainloop is None:  # pragma: no cover - conftest normally loads it
    _spec = importlib.util.spec_from_file_location("_blueferry_lint_mainloop", _PATH)
    assert _spec is not None and _spec.loader is not None
    lint_mainloop = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = lint_mainloop
    _spec.loader.exec_module(lint_mainloop)


def test_source_tree_has_no_new_findings_or_stale_entries() -> None:
    remaining, _allowed, stale = lint_mainloop.lint()
    assert [str(finding) for finding in remaining] == []
    assert stale == set()


def test_every_exemption_has_a_reason() -> None:
    for table in (
        lint_mainloop.ALLOWLIST, lint_mainloop.KNOWN_DEBT, lint_mainloop.RUNTIME_KNOWN_DEBT,
    ):
        assert all(reason.strip() for reason in table.values())
    assert not set(lint_mainloop.ALLOWLIST) & set(lint_mainloop.KNOWN_DEBT)


def _package(tmp_path: Path, **modules: str) -> Path:
    package = tmp_path / "blueferry"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for name, source in modules.items():
        (package / f"{name}.py").write_text(textwrap.dedent(source))
    return package


def _rules(package: Path) -> list[tuple[str, str, str]]:
    remaining, _allowed, _stale = lint_mainloop.lint(package)
    return sorted((f.module, f.function, f.rule) for f in remaining)


def test_rules_fire_only_where_the_daemon_runs(tmp_path) -> None:
    package = _package(
        tmp_path,
        daemon="""
            import sqlite3
            import subprocess
            import time
            from blueferry import loop_code

            def sync(proxy):
                proxy.StartNotify(timeout=5)

            def fine(proxy, done, failed):
                proxy.StartNotify(reply_handler=done, error_handler=failed)
                sqlite3.connect("db", timeout=0.5)
                subprocess.run(["true"], timeout=1)

            def blocking():
                time.sleep(1)
                subprocess.run(["true"])
                sqlite3.connect("db")
        """,
        loop_code="""
            def call(bus):
                bus.call_blocking("a", "/", "i", "M", "", ())
        """,
        cli="""
            import dbus

            def run(proxy, char, done, failed):
                proxy.Ping()
                char.WriteValue(b"x", {}, reply_handler=done, error_handler=failed)
                dbus.Dictionary({})
                proxy.bus.call_async("a", "/", "i", "M", "a{sv}", ({},), done, failed)
        """,
    )
    assert _rules(package) == [
        ("blueferry.cli", "run", "untyped-empty"),
        ("blueferry.cli", "run", "untyped-empty"),
        ("blueferry.cli", "run", "untyped-empty"),
        ("blueferry.daemon", "blocking", "blocking"),
        ("blueferry.daemon", "blocking", "blocking"),
        ("blueferry.daemon", "blocking", "blocking"),
        ("blueferry.daemon", "sync", "sync-dbus"),
        ("blueferry.loop_code", "call", "sync-dbus"),
    ]


def test_function_local_imports_do_not_pull_modules_onto_the_loop(tmp_path) -> None:
    package = _package(
        tmp_path,
        daemon="""
            def later():
                from blueferry import cli_only
        """,
        cli_only="""
            def run(proxy):
                proxy.Ping()
        """,
    )
    assert _rules(package) == []


def test_bus_helpers_and_gio_sync_calls_are_reported_on_the_loop(tmp_path) -> None:
    package = _package(
        tmp_path,
        daemon="""
            from gi.repository import Gio

            def helpers(bus, name):
                bus.release_name(name)
                bus.list_names()
                bus.name_has_owner(name)
                bus.get_name_owner(name)

            def gio(connection):
                Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
                connection.call_sync("a", "/", "i", "M", None, None, 0, -1, None)

            class Service:
                def release_name(self):
                    pass

                def close(self):
                    self.release_name()  # our own method, not the bus helper
        """,
        cli="""
            def run(bus):
                bus.list_names()
        """,
    )
    assert _rules(package) == [
        ("blueferry.daemon", "gio", "blocking"),
        ("blueferry.daemon", "gio", "blocking"),
        *[("blueferry.daemon", "helpers", "sync-dbus")] * 4,
    ]


def test_imported_aliases_of_blocking_calls_are_reported(tmp_path) -> None:
    package = _package(
        tmp_path,
        daemon="""
            import subprocess as sp
            import time as clock
            from sqlite3 import connect
            from subprocess import run as run_command
            from time import sleep
            from time import sleep as nap

            def bare():
                sleep(1)

            def renamed():
                nap(1)

            def module_alias():
                clock.sleep(1)

            def subprocess_aliases():
                sp.run(["true"])
                run_command(["true"])
                sp.run(["true"], timeout=1)

            def sqlite_alias(path):
                connect(path)

            def unrelated(sleep_ms):
                sleep_ms(1)
        """,
    )
    assert _rules(package) == [
        ("blueferry.daemon", "bare", "blocking"),
        ("blueferry.daemon", "module_alias", "blocking"),
        ("blueferry.daemon", "renamed", "blocking"),
        ("blueferry.daemon", "sqlite_alias", "blocking"),
        ("blueferry.daemon", "subprocess_aliases", "blocking"),
        ("blueferry.daemon", "subprocess_aliases", "blocking"),
    ]
