"""The conftest GLib source guard sees every real timer a test can arm."""
from __future__ import annotations

import inspect
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from gi.repository import GLib

from blueferry.adapter_class_supervisor import AdapterClassSupervisor
from blueferry.ancs.client import AncsClient
from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.bluetooth_recovery import BluetoothRecovery
from blueferry.event_dispatcher import EventDispatcher
from blueferry.obex.mns_watch import MnsWatch
from blueferry.profile_supervisor import ProfileSupervisor
from blueferry.solicitation_supervisor import SolicitationSupervisor


@pytest.mark.parametrize(("owner", "parameter", "wrapped_name"), [
    (AdapterClassSupervisor, "schedule", "timeout_add_seconds"),
    (AncsClient, "schedule", "timeout_add_seconds"),
    (BearerSupervisor, "schedule", "timeout_add_seconds"),
    (BluetoothRecovery, "schedule", "timeout_add_seconds"),
    (BluetoothRecovery, "idle", "idle_add"),
    (EventDispatcher, "schedule", "timeout_add_seconds"),
    (MnsWatch, "schedule", "timeout_add_seconds"),
    (ProfileSupervisor, "schedule", "timeout_add_seconds"),
    (SolicitationSupervisor, "schedule", "timeout_add_seconds"),
])
def test_import_time_scheduler_defaults_are_recorded(
    owner, parameter, wrapped_name,
) -> None:
    # These defaults are bound when the module is imported. The guard only
    # covers them because conftest wraps GLib before blueferry is imported.
    default = inspect.signature(owner).parameters[parameter].default
    assert default is getattr(GLib, wrapped_name)
    assert inspect.unwrap(default) is not default


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
