"""Recovery must fail closed without ever touching the host's Bluetooth."""
from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from blueferry import bluetooth_recovery as mod
from blueferry.settings_store import SettingsStore

PHONE = "11:22:33:44:55:66"
RADIO = "AA:BB:CC:DD:EE:FF"


class Worker:
    def __init__(self):
        self.jobs = []
        self.reserved = False
        self.busy = False

    def submit(self, operation, *, on_success=None, on_error=None, reserved=False):
        assert self.reserved == reserved
        self.jobs.append((operation, on_success, on_error))

    def finish(self, error=None):
        operation, success, failure = self.jobs.pop(0)
        if error:
            failure(error)
        else:
            try:
                result = operation()
            except Exception as exc:
                failure(exc)
            else:
                success(result)

    def reserve_if_idle(self):
        if self.busy or self.jobs or self.reserved:
            return False
        self.reserved = True
        return True

    def release(self):
        self.reserved = False


class Harness:
    def __init__(self, tmp_path):
        self.now = 10000.0
        self.wall = 100000.0
        self.state = mod.AdapterState(":1.2", RADIO, True, True)
        self.observed = mod.RecoveryObservation(False, None, True)
        self.settings = SettingsStore(tmp_path / "settings.json")
        self.settings.update(**{mod.SETTINGS_KEY: {
            "adapter": RADIO, "phone": PHONE, "verified": True,
            "spent": False, "last_attempt": 0,
        }})
        self.worker = Worker()
        self.calls = []
        self.idles = []
        self.create()

    def create(self, adapter=None):
        self.recovery = mod.BluetoothRecovery(
            PHONE,
            adapter or SimpleNamespace(read=lambda: self.state, cycle=self.cycle,
                            restore_pending=False, restore=self.restore,
                            start_monitoring=lambda: None, stop_monitoring=lambda: None),
            self.worker,
            observe=lambda: self.observed,
            probe=lambda: self.calls.append("probe"),
            probe_health=lambda: self.calls.append("probe-health"),
            reset_le=lambda: self.calls.append("reset-le"),
            pause=lambda: self.calls.append("pause"),
            resume=lambda: self.calls.append("resume"),
            settings=self.settings,
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
            schedule=lambda *_: 1,
            idle=lambda callback, *args: self.idles.append((callback, args)),
            cancel=lambda *_: None,
        )
        self.recovery.start()

    def cycle(self, expected, cancelled):
        assert self.settings.read()[mod.SETTINGS_KEY]["spent"] is True
        assert self.worker.reserved
        if cancelled.is_set():
            raise RuntimeError("cancelled")
        assert expected == self.state
        self.calls.append("cycle")

    def restore(self):
        assert self.worker.reserved
        self.calls.append("restore")
        self.recovery.adapter.restore_pending = False

    def tick(self, seconds=0):
        remaining = seconds
        while True:
            step = min(mod.POLL_SECONDS, remaining)
            self.now += step
            self.wall += step
            self.recovery._tick()
            remaining -= step
            if remaining <= 0:
                break

    def ready_to_probe(self):
        self.tick()
        self.tick(mod.SOFT_RESET_SECONDS)
        self.tick(mod.OUTAGE_SECONDS - mod.SOFT_RESET_SECONDS)

    def dispatch(self):
        while self.idles:
            callback, args = self.idles.pop(0)
            callback(*args)

    def prepare_cycle(self):
        self.ready_to_probe()
        self.worker.finish()
        self.dispatch()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_requires_dwell_soft_reset_and_fresh_remote_probe(h):
    h.tick()
    h.tick(179)
    assert not h.calls and not h.worker.jobs
    h.tick(1)
    assert h.calls == ["reset-le"]
    h.tick(119)
    assert not h.worker.jobs
    h.tick(1)
    assert len(h.worker.jobs) == 1
    assert not h.recovery.active
    h.worker.finish()
    assert h.calls == ["reset-le", "probe"]
    h.dispatch()
    assert h.recovery.active
    assert h.calls[-1] == "pause"
    h.worker.finish()
    assert h.calls[-2:] == ["cycle", "resume"]
    assert not h.worker.reserved


@pytest.mark.parametrize("failure", [None, RuntimeError("power refused")])
def test_one_attempt_persists_across_restarts_even_after_failure(h, failure):
    h.prepare_cycle()
    h.worker.finish(failure)
    h.recovery.stop()
    h.create()
    for _ in range(5):
        h.tick(3600)
    assert not h.worker.jobs
    assert h.calls.count("pause") == 1


@pytest.mark.parametrize("unsafe", ["other-device", "off", "busy", "unverified", "new-radio"])
def test_no_attempt_without_all_prerequisites(h, unsafe):
    if unsafe == "other-device":
        h.state = replace(h.state, safe=False)
    elif unsafe == "off":
        h.state = replace(h.state, powered=False, safe=False)
    elif unsafe == "busy":
        h.observed = replace(h.observed, eligible=False)
    elif unsafe == "unverified":
        h.recovery._record["verified"] = False
    else:
        h.state = replace(h.state, address="00:00:00:00:00:01")
    h.ready_to_probe()
    assert not h.worker.jobs and "pause" not in h.calls


def test_failed_map_probe_never_cycles(h):
    h.ready_to_probe()
    h.worker.finish(RuntimeError("phone out of range"))
    h.dispatch()
    assert not h.recovery.active
    assert h.settings.read()[mod.SETTINGS_KEY]["spent"] is False
    h.tick(10)
    assert not h.worker.jobs


@pytest.mark.parametrize("change", ["other-device", "owner", "healthy", "busy", "stale", "sleep", "stop"])
def test_revalidates_after_probe_and_ignores_stale_callbacks(h, change):
    h.ready_to_probe()
    h.worker.finish()
    if change == "other-device":
        h.state = replace(h.state, safe=False)
    elif change == "owner":
        h.state = replace(h.state, owner=":1.3")
    elif change == "healthy":
        h.observed = replace(h.observed, healthy=True, health_proof=h.now)
    elif change == "busy":
        h.worker.busy = True
    elif change == "stale":
        h.now += 31
    elif change == "sleep":
        h.recovery.invalidate(suspended=True)
    else:
        h.recovery.stop()
    h.dispatch()
    assert "pause" not in h.calls
    assert not h.worker.jobs and not h.worker.reserved


def test_failed_budget_write_prevents_power_off(h, monkeypatch):
    def fail(**_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(h.settings, "update", fail)
    h.prepare_cycle()
    assert "pause" not in h.calls and not h.worker.reserved


def test_only_fresh_sustained_health_rearms_and_hourly_limit_still_applies(h):
    h.prepare_cycle()
    h.worker.finish()
    h.observed = replace(h.observed, healthy=True, health_proof=h.now)
    h.tick()
    h.tick(600)  # A cached connected flag and old proof cannot rearm recovery.
    assert h.settings.read()[mod.SETTINGS_KEY]["spent"] is True
    for _ in range(61):
        h.observed = replace(h.observed, health_proof=h.now + 10)
        h.tick(10)
    assert h.settings.read()[mod.SETTINGS_KEY]["spent"] is False
    h.observed = replace(h.observed, healthy=False)
    h.ready_to_probe()
    assert not h.worker.jobs
    h.tick(3600)
    h.tick(300)
    h.tick(60)
    assert h.worker.jobs


def test_suspend_and_manual_power_off_restart_the_observation_window(h):
    h.tick()
    h.tick(179)
    h.recovery.invalidate(suspended=True)
    h.tick(1000)
    h.recovery.invalidate(suspended=False)
    h.tick()
    assert not h.calls
    h.state = replace(h.state, powered=False)
    h.tick(179)
    h.state = replace(h.state, powered=True)
    h.tick(179)
    assert not h.calls


def test_shutdown_cancels_a_reserved_cycle_before_it_touches_power(h):
    h.prepare_cycle()
    h.recovery.stop()
    h.worker.finish()
    assert "cycle" not in h.calls and "resume" not in h.calls
    assert not h.worker.reserved


def test_new_controller_needs_a_fresh_health_proof(h):
    h.state = replace(h.state, address="00:00:00:00:00:01")
    h.observed = replace(h.observed, healthy=True, health_proof=h.now - 1)
    h.tick()
    assert h.settings.read()[mod.SETTINGS_KEY]["adapter"] == RADIO
    h.observed = replace(h.observed, health_proof=h.now + 10)
    h.tick(10)
    assert h.settings.read()[mod.SETTINGS_KEY]["adapter"] == h.state.address


def test_clock_rollback_and_corrupt_limit_fail_closed(h):
    for last in (h.wall + 1, float("nan"), "yesterday"):
        h.recovery._record["last_attempt"] = last
        h.ready_to_probe()
    assert not h.worker.jobs


@pytest.fixture
def bluez(monkeypatch, tmp_path):
    adapter = mod.BluezRecoveryAdapter("hci1", PHONE,
                                      settings=SettingsStore(tmp_path / "radio-settings.json"))
    props = {"Address": RADIO, "Powered": True, "Discovering": False,
             "Discoverable": False, "PowerState": "on"}
    objects = {adapter.path: {"org.bluez.Adapter1": props},
               adapter.device_path: {"org.bluez.Device1": {"Paired": True}}}
    fake = SimpleNamespace(props=props, objects=objects, writes=[], owner=":1.2", fail_off=False,
                           bus_id="test-system-bus", instance="test-adapter-insertion")

    def set_property(_interface, _name, value, **_kwargs):
        fake.writes.append(bool(value))
        props["Powered"] = bool(value)
        props["PowerState"] = "on" if value else "off"
        if not value and fake.fail_off:
            raise RuntimeError("reply lost after powering off")

    fake.GetManagedObjects = lambda **_kwargs: objects
    fake.GetId = lambda **_kwargs: fake.bus_id
    fake.GetAll = lambda *_args, **_kwargs: props
    fake.Get = lambda _interface, name, **_kwargs: props[name]
    fake.Set = set_property
    monkeypatch.setattr(mod, "get_system_bus", lambda: SimpleNamespace(
        get_name_owner=lambda *_: fake.owner,
        get_object=lambda *_, **_kwargs: fake,
        add_signal_receiver=lambda *_, **_kwargs: SimpleNamespace(remove=lambda: None),
    ))
    monkeypatch.setattr(mod.dbus, "Interface", lambda obj, _interface: obj)
    monkeypatch.setattr(mod.BluezRecoveryAdapter, "_adapter_instance", lambda _self: fake.instance)
    return adapter, fake


def test_adapter_inspection_rejects_other_connected_devices_and_pairing(bluez):
    adapter, fake = bluez
    assert adapter.read().safe
    other = adapter.path + "/dev_00_00_00_00_00_01"
    fake.objects[other] = {"org.bluez.Device1": {"Connected": True}}
    assert not adapter.read().safe
    fake.objects[other]["org.bluez.Device1"]["Connected"] = False
    assert adapter.read().safe
    fake.props["Discovering"] = True
    assert not adapter.read().safe


@pytest.mark.parametrize("fail_off", [False, True])
def test_cycle_restores_power_even_when_power_off_reply_is_lost(bluez, fail_off):
    adapter, fake = bluez
    fake.fail_off = fail_off
    before = adapter.read()
    if fail_off:
        with pytest.raises(RuntimeError, match="reply lost"):
            adapter.cycle(before, threading.Event())
    else:
        adapter.cycle(before, threading.Event())
    assert fake.writes == [False, True]
    assert fake.props["Powered"]


@pytest.mark.parametrize("change", ["owner", "address", "powered", "cancel"])
def test_cycle_rechecks_controller_identity_and_cancellation(bluez, change):
    adapter, fake = bluez
    before = adapter.read()
    cancelled = threading.Event()
    if change == "owner":
        fake.owner = ":1.3"
    elif change == "address":
        fake.props["Address"] = "00:00:00:00:00:01"
    elif change == "powered":
        fake.props["Powered"] = False
    else:
        cancelled.set()
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, cancelled)
    assert not fake.writes


def test_replacement_controller_is_never_powered_on(bluez):
    adapter, fake = bluez
    before = adapter.read()
    original_set = fake.Set

    def swap(*args, **kwargs):
        original_set(*args, **kwargs)
        fake.props["Address"] = "00:00:00:00:00:01"

    fake.Set = swap
    with pytest.raises(RuntimeError, match="controller changed"):
        adapter.cycle(before, threading.Event())
    assert fake.writes == [False]


def test_rfkill_during_cycle_is_not_overridden(bluez):
    adapter, fake = bluez
    before = adapter.read()
    original_set = fake.Set

    def block(*args, **kwargs):
        original_set(*args, **kwargs)
        fake.props["PowerState"] = "off-blocked"

    fake.Set = block
    with pytest.raises(RuntimeError, match="could not be restored"):
        adapter.cycle(before, threading.Event())
    assert fake.writes == [False]


def test_sleep_during_recovery_defers_reconnect_until_resume(h):
    h.prepare_cycle()
    h.recovery.invalidate(suspended=True)
    h.worker.finish()
    assert "resume" not in h.calls
    h.recovery.invalidate(suspended=False)
    assert h.calls[-1] == "resume"
    h.recovery.invalidate(suspended=False)
    assert h.calls.count("resume") == 1


def test_map_probe_uses_remote_folder_listing_without_changing_folder(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "obex", lambda path, iface: SimpleNamespace(
        ListFolders=lambda filters, **kwargs: calls.append((path, iface, filters, kwargs)),
    ))
    mod.probe_map("/session1")
    assert calls == [("/session1", "org.bluez.obex.MessageAccess1", {"MaxCount": 1}, {"timeout": 15.0})]


def test_soft_recovery_busy_window_does_not_restart_its_own_outage(h):
    h.tick()
    h.tick(180)
    h.observed = replace(h.observed, eligible=False, busy=True)
    h.tick(45)
    h.observed = replace(h.observed, eligible=True, busy=False)
    h.tick(75)
    assert h.calls == ["reset-le"]
    assert len(h.worker.jobs) == 1


def test_stalled_main_loop_cannot_count_as_continuous_failure(h):
    h.tick()
    h.now += 3600
    h.wall += 3600
    h.tick()
    assert not h.calls and not h.worker.jobs


def test_already_armed_healthy_phone_does_not_get_periodic_probes(h):
    h.observed = replace(h.observed, healthy=True)
    h.tick(3600)
    assert "probe-health" not in h.calls


def test_removed_bond_forgets_eligibility_across_restart(h):
    h.recovery.forget_phone()
    h.recovery.stop()
    h.create()
    h.ready_to_probe()
    assert not h.calls and not h.worker.jobs


def test_timed_out_power_off_waits_for_transition_before_restoring(bluez):
    adapter, fake = bluez
    before = adapter.read()
    original_set = fake.Set

    def delayed_off(interface, name, value, **kwargs):
        if value:
            original_set(interface, name, value, **kwargs)
            return
        fake.writes.append(False)
        fake.props["PowerState"] = "on-disabling"
        raise RuntimeError("off reply timed out")

    fake.Set = delayed_off
    with pytest.raises(RuntimeError, match="off reply timed out"):
        adapter.cycle(before, threading.Event())
    assert adapter.restore_pending
    assert fake.writes == [False]
    # Still pending long after the original D-Bus deadline: never send another
    # power-off or mistake Powered=true/on-disabling for restored power.
    for _ in range(5):
        adapter.restore()
    assert adapter.restore_pending
    assert fake.writes == [False]
    fake.props.update(Powered=False, PowerState="off")
    adapter.restore()
    assert fake.writes == [False, True]
    assert not adapter.restore_pending


def test_shutdown_restoration_wait_is_bounded(bluez, monkeypatch):
    adapter, fake = bluez
    adapter._restore = mod._PowerRestore(adapter.read(), fake.bus_id, fake.instance)
    fake.props["PowerState"] = "on-disabling"
    times = iter([100.0, 101.0, 131.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(mod.time, "sleep", lambda _delay: None)
    adapter.finish_shutdown()
    assert adapter.restore_pending
    assert not fake.writes


@pytest.mark.parametrize("failure", ["read-off", "power-on"])
def test_transient_restore_failure_is_retried_without_another_cycle(h, bluez, failure):
    adapter, fake = bluez
    h.recovery.adapter = adapter
    h.prepare_cycle()
    original_read = fake.GetManagedObjects
    original_set = fake.Set
    failed = False

    def read(**kwargs):
        nonlocal failed
        if not failed and ((failure == "read-off" and fake.writes == [False])
                           or (failure == "read-on" and fake.writes == [False, True])):
            failed = True
            raise RuntimeError("temporary read failure")
        return original_read(**kwargs)

    def power(interface, name, value, **kwargs):
        nonlocal failed
        if failure == "power-on" and value and not failed:
            failed = True
            raise RuntimeError("temporary Set failure before delivery")
        original_set(interface, name, value, **kwargs)

    fake.GetManagedObjects = read
    fake.Set = power
    h.worker.finish()
    assert failed and adapter.restore_pending
    assert h.recovery.active and h.worker.reserved
    assert "resume" not in h.calls
    record = h.settings.read()[mod.SETTINGS_KEY]
    h.tick(10)
    h.worker.finish()
    assert not adapter.restore_pending
    assert not h.recovery.active and not h.worker.reserved
    assert h.calls[-1] == "resume"
    assert fake.writes == [False, True]
    assert fake.props["Powered"]
    assert h.settings.read()[mod.SETTINGS_KEY] == record
    h.tick(7200)
    assert not h.worker.jobs


def test_late_power_off_keeps_the_worker_reserved_until_restored(h, bluez):
    adapter, fake = bluez
    h.recovery.adapter = adapter
    h.prepare_cycle()
    original_set = fake.Set

    def late_off(interface, name, value, **kwargs):
        if value:
            original_set(interface, name, value, **kwargs)
        else:
            fake.writes.append(False)
            fake.props["PowerState"] = "on-disabling"
            raise RuntimeError("off reply timed out")

    fake.Set = late_off
    h.worker.finish()
    for _ in range(12):
        h.tick(10)
        assert len(h.worker.jobs) == 1
        h.worker.finish()
        assert h.recovery.active and h.worker.reserved
        assert "resume" not in h.calls
        assert fake.writes == [False]
    fake.props.update(Powered=False, PowerState="off")
    h.tick(10)
    h.worker.finish()
    assert fake.writes == [False, True]
    assert not h.recovery.active and not h.worker.reserved
    assert h.calls.count("pause") == h.calls.count("resume") == 1


@pytest.mark.parametrize("change", ["owner", "address", "replug", "connection", "discovery"])
def test_proxy_creation_cannot_hide_changes_before_power_off(bluez, monkeypatch, change):
    adapter, fake = bluez
    before = adapter.read()
    peer = {"Connected": False, "Paired": False}
    fake.objects[adapter.path + "/dev_00_00_00_00_00_01"] = {"org.bluez.Device1": peer}
    bus = mod.get_system_bus()

    def get_object(_owner, path, **kwargs):
        assert kwargs == {"introspect": False}
        if path == adapter.path:
            if change == "owner":
                fake.owner = ":1.3"
            elif change == "address":
                fake.props["Address"] = "00:00:00:00:00:99"
            elif change == "replug":
                # Even reinserting the same controller invalidates the snapshot.
                adapter._interfaces_changed(adapter.path, ["org.bluez.Adapter1"])
            elif change == "connection":
                peer["Connected"] = True
            else:
                fake.props["Discovering"] = True
        return fake

    bus.get_object = get_object
    monkeypatch.setattr(mod, "get_system_bus", lambda: bus)
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes and not adapter.restore_pending


def test_signal_after_snapshot_cancels_power_request(bluez, monkeypatch):
    adapter, fake = bluez
    before = adapter.read()
    original_read = adapter.read

    def read():
        result = original_read()
        adapter._properties_changed("org.bluez.Device1", {"Connected": True}, [],
                                    path=adapter.path + "/dev_00_00_00_00_00_01")
        return result

    monkeypatch.setattr(adapter, "read", read)
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes


@pytest.mark.parametrize("bond", ["Paired", "Bonded"])
def test_disconnected_bonded_peer_disables_automatic_cycling(bluez, bond):
    adapter, fake = bluez
    fake.objects[adapter.path + "/dev_00_00_00_00_00_01"] = {
        "org.bluez.Device1": {"Connected": False, bond: True},
    }
    before = adapter.read()
    assert not before.safe
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes


def test_missing_power_transition_property_disables_cycling(bluez):
    adapter, fake = bluez
    fake.props.pop("PowerState")
    before = adapter.read()
    assert not before.safe
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes


@pytest.mark.parametrize("change", ["owner", "replug", "rfkill"])
def test_pending_restoration_abandons_a_replaced_or_blocked_controller(h, bluez, change):
    adapter, fake = bluez
    h.recovery.adapter = adapter
    h.prepare_cycle()
    original_set = fake.Set

    def power(interface, name, value, **kwargs):
        if value:
            raise RuntimeError("temporary failure")
        original_set(interface, name, value, **kwargs)

    fake.Set = power
    h.worker.finish()
    assert h.recovery.active and adapter.restore_pending
    fake.Set = original_set
    if change == "owner":
        adapter._owner_changed("org.bluez", fake.owner, ":1.3")
    elif change == "replug":
        adapter._interfaces_changed(adapter.path, ["org.bluez.Adapter1"])
    else:
        fake.props["PowerState"] = "off-blocked"
    h.tick(10)
    h.worker.finish()
    assert fake.writes == [False]
    assert not adapter.restore_pending and not h.recovery.active
    assert not h.worker.reserved


def test_successful_power_on_followed_by_user_power_off_is_not_reversed(bluez):
    adapter, fake = bluez
    before = adapter.read()
    original_read = fake.GetManagedObjects

    def read(**kwargs):
        if fake.writes == [False, True]:
            raise RuntimeError("temporary read failure")
        return original_read(**kwargs)

    fake.GetManagedObjects = read
    adapter.cycle(before, threading.Event())
    assert not adapter.restore_pending
    fake.GetManagedObjects = original_read
    fake.props.update(Powered=False, PowerState="off")
    adapter.restore()
    assert not adapter.restore_pending
    assert fake.writes == [False, True]
    assert not fake.props["Powered"]


def test_shutdown_retries_restoration_after_the_main_loop_stops(bluez):
    adapter, fake = bluez
    adapter._restore = mod._PowerRestore(adapter.read(), fake.bus_id, fake.instance)
    fake.props.update(Powered=False, PowerState="off")
    adapter.finish_shutdown()
    assert fake.writes == [True]
    assert fake.props["Powered"] and not adapter.restore_pending


def test_cancel_after_power_off_still_restores_the_radio(bluez):
    adapter, fake = bluez
    before = adapter.read()
    cancelled = threading.Event()
    original_set = fake.Set

    def power(*args, **kwargs):
        original_set(*args, **kwargs)
        cancelled.set()

    fake.Set = power
    adapter.cycle(before, cancelled)
    assert fake.writes == [False, True]
    assert not adapter.restore_pending


def test_suspend_defers_pending_restore_work_until_resume(h):
    h.prepare_cycle()
    h.recovery.adapter.restore_pending = True
    h.worker.finish(RuntimeError("restore delayed"))
    h.recovery.invalidate(suspended=True)
    h.tick(100)
    assert not h.worker.jobs
    assert h.recovery.active and h.worker.reserved
    h.recovery.invalidate(suspended=False)
    assert "resume" not in h.calls
    h.tick(10)
    h.worker.finish()
    assert h.calls[-2:] == ["restore", "resume"]


@pytest.mark.parametrize("event", ["adapter-property", "target-bond", "peer-bearer", "peer-added"])
def test_monitored_changes_invalidate_even_an_identical_later_snapshot(bluez, monkeypatch, event):
    adapter, fake = bluez
    bus = mod.get_system_bus()
    signals = {}
    removed = []

    def subscribe(callback, **kwargs):
        signals[kwargs["signal_name"]] = callback
        return SimpleNamespace(remove=lambda: removed.append(kwargs["signal_name"]))

    bus.add_signal_receiver = subscribe
    monkeypatch.setattr(mod, "get_system_bus", lambda: bus)
    adapter.start_monitoring()
    adapter.start_monitoring()
    before = adapter.read()
    # Signals may report changes which reverted before the final snapshot.
    # The guard must remember these events, not merely compare property values.
    properties = signals["PropertiesChanged"]
    if event == "adapter-property":
        properties("org.bluez.Adapter1", {}, ["Discovering"], path=adapter.path)
    elif event == "target-bond":
        properties("org.bluez.Device1", {"Bonded": False}, [], path=adapter.device_path)
    elif event == "peer-bearer":
        properties("org.bluez.Bearer.LE1", {"Connected": True}, [],
                   path=adapter.path + "/dev_00_00_00_00_00_01")
    else:
        signals["InterfacesAdded"](adapter.path + "/dev_00_00_00_00_00_01",
                                   {"org.bluez.Device1": {"Connected": False}})
    assert adapter.read().safe
    with pytest.raises(RuntimeError, match="conditions changed"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes
    adapter.stop_monitoring()
    adapter.stop_monitoring()
    assert sorted(removed) == sorted(signals)


def test_unrelated_device_events_do_not_invalidate_recovery(bluez):
    adapter, fake = bluez
    before = adapter.read()
    adapter._interfaces_changed("/org/bluez/hci0", ["org.bluez.Adapter1"])
    adapter._properties_changed("org.bluez.Adapter1", {"Powered": False}, [],
                                path="/org/bluez/hci0")
    adapter._properties_changed("org.bluez.Device1", {"RSSI": -50}, [],
                                path=adapter.device_path)
    adapter._properties_changed("org.bluez.Device1", {"Connected": True}, [],
                                path=adapter.device_path)
    adapter.cycle(before, threading.Event())
    assert fake.writes == [False, True]


def test_monitor_registration_failure_cleans_up_and_does_not_enable_recovery(h, bluez, monkeypatch):
    adapter, _fake = bluez
    h.recovery.stop()
    h.recovery.adapter = adapter
    bus = mod.get_system_bus()
    calls = []

    def subscribe(_callback, **_kwargs):
        calls.append("subscribe")
        if len(calls) == 2:
            raise RuntimeError("subscription failed")
        return SimpleNamespace(remove=lambda: calls.append("remove"))

    bus.add_signal_receiver = subscribe
    monkeypatch.setattr(mod, "get_system_bus", lambda: bus)
    with pytest.raises(RuntimeError, match="subscription failed"):
        h.recovery.start()
    h.ready_to_probe()
    assert calls == ["subscribe", "subscribe", "remove"]
    assert not h.worker.jobs


def test_signal_during_inspection_discards_the_snapshot(bluez):
    adapter, fake = bluez
    original_read = fake.GetManagedObjects

    def read(**kwargs):
        objects = original_read(**kwargs)
        adapter._properties_changed("org.bluez.Adapter1", {"Discoverable": True}, [],
                                    path=adapter.path)
        return objects

    fake.GetManagedObjects = read
    with pytest.raises(RuntimeError, match="changed during inspection"):
        adapter.read()


def test_shutdown_still_restores_after_a_read_error(bluez, monkeypatch):
    adapter, fake = bluez
    adapter._restore = mod._PowerRestore(adapter.read(), fake.bus_id, fake.instance)
    fake.props.update(Powered=False, PowerState="off")
    original_read = fake.GetManagedObjects

    def fail_once(**_kwargs):
        fake.GetManagedObjects = original_read
        raise RuntimeError("temporary read failure")

    fake.GetManagedObjects = fail_once
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    adapter.finish_shutdown()
    assert fake.writes == [True]
    assert not adapter.restore_pending


def test_second_power_cycle_is_refused_while_restoration_is_pending(bluez):
    adapter, fake = bluez
    before = adapter.read()
    adapter._restore = mod._PowerRestore(before, fake.bus_id, fake.instance)
    with pytest.raises(RuntimeError, match="restoration is still pending"):
        adapter.cycle(before, threading.Event())
    assert not fake.writes


@pytest.mark.parametrize("signal_before_error", [False, True])
def test_observed_power_on_completes_a_lost_reply_before_user_power_off(bluez, signal_before_error):
    adapter, fake = bluez
    expected = adapter.read()
    original_set = fake.Set

    def lost_on_reply(interface, name, value, **kwargs):
        original_set(interface, name, value, **kwargs)
        if value:
            if signal_before_error:
                adapter._properties_changed("org.bluez.Adapter1", {"Powered": True}, [],
                                            path=adapter.path)
            raise RuntimeError("power-on reply lost after success")

    fake.Set = lost_on_reply
    with pytest.raises(RuntimeError, match="reply lost"):
        adapter.cycle(expected, threading.Event())
    if not signal_before_error:
        assert adapter.restore_pending
        adapter._properties_changed("org.bluez.Adapter1", {"Powered": True}, [],
                                    path=adapter.path)
    assert not adapter.restore_pending
    fake.props.update(Powered=False, PowerState="off")
    adapter._properties_changed("org.bluez.Adapter1", {"Powered": False}, [], path=adapter.path)
    fake.Set = original_set
    adapter.restore()
    assert fake.writes == [False, True]
    assert not fake.props["Powered"]
    restarted = mod.BluezRecoveryAdapter("hci1", PHONE, settings=adapter._settings)
    assert not restarted.restore_pending


def test_old_on_signal_does_not_discard_a_pending_power_off(bluez):
    adapter, fake = bluez
    expected = adapter.read()

    def delayed_off(*_args, **_kwargs):
        fake.writes.append(False)
        fake.props["PowerState"] = "on-disabling"
        adapter._properties_changed("org.bluez.Adapter1", {"Powered": True}, [], path=adapter.path)
        raise RuntimeError("off reply timed out")

    fake.Set = delayed_off
    with pytest.raises(RuntimeError, match="timed out"):
        adapter.cycle(expected, threading.Event())
    assert adapter.restore_pending


def test_shutdown_deadline_keeps_restoration_durable_for_the_next_daemon(h, bluez, monkeypatch):
    adapter, fake = bluez
    h.recovery.adapter = adapter
    h.prepare_cycle()
    original_set = fake.Set

    def delayed_off(_interface, _name, value, **_kwargs):
        assert not value
        fake.writes.append(False)
        fake.props["PowerState"] = "on-disabling"
        raise RuntimeError("off reply timed out")

    fake.Set = delayed_off
    h.worker.finish()
    h.recovery.stop()
    times = iter([100, 101, 131])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    adapter.finish_shutdown()
    assert adapter.restore_pending
    budget = h.settings.read()[mod.SETTINGS_KEY]
    fake.props.update(Powered=False, PowerState="off")
    fake.Set = original_set
    # New adapter, recovery coordinator, and worker; only disk state survives.
    restarted = mod.BluezRecoveryAdapter("hci1", PHONE, settings=adapter._settings)
    assert restarted.restore_pending
    h.worker = Worker()
    h.create(adapter=restarted)
    assert h.recovery.active and h.worker.reserved
    assert h.calls[-1] == "pause"
    h.worker.finish()
    assert fake.writes == [False, True]
    assert fake.props["Powered"] and not restarted.restore_pending
    assert not h.recovery.active and not h.worker.reserved
    assert h.calls[-1] == "resume"
    assert h.settings.read()[mod.SETTINGS_KEY] == budget
    assert adapter._settings.read()[mod.BLUETOOTH_RESTORE_KEY] is None


@pytest.mark.parametrize("change", ["bus", "owner", "address", "insertion", "missing", "rfkill", "phone", "path"])
def test_durable_restoration_never_targets_a_changed_identity(bluez, change):
    adapter, fake = bluez
    expected = adapter.read()
    original_set = fake.Set

    def fail_on(interface, name, value, **kwargs):
        if value:
            raise RuntimeError("power-on temporarily failed")
        original_set(interface, name, value, **kwargs)

    fake.Set = fail_on
    with pytest.raises(RuntimeError, match="temporarily failed"):
        adapter.cycle(expected, threading.Event())
    fake.Set = original_set
    if change == "bus":
        fake.bus_id = "replacement-system-bus"
    elif change == "owner":
        fake.owner = ":1.99"
    elif change == "address":
        fake.props["Address"] = "00:00:00:00:00:99"
    elif change == "insertion":
        fake.instance = "same-dongle-new-insertion"
    elif change == "missing":
        del fake.objects[adapter.path]
    elif change == "rfkill":
        fake.props["PowerState"] = "off-blocked"
    restarted = mod.BluezRecoveryAdapter(
        "hci2" if change == "path" else "hci1",
        "00:00:00:00:00:01" if change == "phone" else PHONE,
        settings=adapter._settings,
    )
    if change in {"phone", "path"}:
        restarted.restore()  # Invalid journal is removed without any D-Bus mutation.
    else:
        with pytest.raises(RuntimeError):
            restarted.restore()
    assert not restarted.restore_pending
    assert fake.writes == [False]
    assert adapter._settings.read()[mod.BLUETOOTH_RESTORE_KEY] is None


def test_failed_journal_write_prevents_power_off(bluez, monkeypatch):
    adapter, fake = bluez

    def fail(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(adapter._settings, "update", fail)
    with pytest.raises(OSError, match="disk full"):
        adapter.cycle(adapter.read(), threading.Event())
    assert not fake.writes and not adapter.restore_pending


@pytest.mark.parametrize("on_was_delivered", [False, True])
def test_restart_finishes_an_unacknowledged_power_on_without_another_cycle(bluez, on_was_delivered):
    adapter, fake = bluez
    original_set = fake.Set

    def uncertain_on(interface, name, value, **kwargs):
        if value:
            if on_was_delivered:
                original_set(interface, name, value, **kwargs)
            raise RuntimeError("no power-on reply")
        original_set(interface, name, value, **kwargs)

    fake.Set = uncertain_on
    with pytest.raises(RuntimeError, match="no power-on reply"):
        adapter.cycle(adapter.read(), threading.Event())
    assert adapter.restore_pending
    fake.Set = original_set
    restarted = mod.BluezRecoveryAdapter("hci1", PHONE, settings=adapter._settings)
    restarted.restore()
    assert fake.writes == [False, True]
    assert fake.props["Powered"]
    assert not restarted.restore_pending
    assert adapter._settings.read()[mod.BLUETOOTH_RESTORE_KEY] is None


@pytest.mark.parametrize("via_signal", [False, True])
def test_failed_completion_write_retries_only_the_journal_after_user_power_off(bluez, monkeypatch, via_signal):
    adapter, fake = bluez
    original_update = adapter._settings.update
    original_set = fake.Set

    def fail_clear(**kwargs):
        if kwargs.get(mod.BLUETOOTH_RESTORE_KEY, "absent") is None:
            raise OSError("cannot clear journal")
        original_update(**kwargs)

    def power(interface, name, value, **kwargs):
        original_set(interface, name, value, **kwargs)
        if value and via_signal:
            adapter._properties_changed("org.bluez.Adapter1", {"Powered": True}, [], path=adapter.path)
            raise RuntimeError("on reply lost")

    fake.Set = power
    monkeypatch.setattr(adapter._settings, "update", fail_clear)
    with pytest.raises((OSError, RuntimeError)):
        adapter.cycle(adapter.read(), threading.Event())
    assert adapter.restore_pending
    fake.props.update(Powered=False, PowerState="off")
    monkeypatch.setattr(adapter._settings, "update", original_update)
    adapter.restore()
    assert not adapter.restore_pending
    assert fake.writes == [False, True]
    assert not fake.props["Powered"]


def test_startup_restoration_waits_for_an_idle_worker_without_spending_a_new_attempt(h):
    h.recovery.stop()
    h.worker.busy = True
    h.recovery.adapter.restore_pending = True
    h.recovery.start()
    assert not h.worker.jobs and not h.recovery.active
    h.worker.busy = False
    h.tick(10)
    assert h.recovery.active and h.worker.reserved
    h.worker.finish()
    assert h.calls == ["pause", "restore", "resume"]
    assert h.settings.read()[mod.SETTINGS_KEY]["spent"] is False


@pytest.mark.parametrize("raw", [{}, [], {"version": 1, "path": "/org/bluez/hci1", "device": "/org/bluez/hci1/dev_11_22_33_44_55_66", "owner": ":1.2", "address": RADIO, "bus_id": "test", "instance": "test", "phase": []}])
def test_malformed_restoration_journal_is_discarded_without_power_changes(bluez, raw):
    adapter, fake = bluez
    adapter._settings.update(**{mod.BLUETOOTH_RESTORE_KEY: raw})
    restarted = mod.BluezRecoveryAdapter("hci1", PHONE, settings=adapter._settings)
    restarted.restore()
    assert not restarted.restore_pending
    assert not fake.writes
