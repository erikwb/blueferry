"""The conftest GLib source guard sees every real timer a test can arm."""
from __future__ import annotations

import inspect

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
