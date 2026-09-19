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

    def create(self):
        self.recovery = mod.BluetoothRecovery(
            PHONE,
            SimpleNamespace(read=lambda: self.state, cycle=self.cycle),
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
def bluez(monkeypatch):
    adapter = mod.BluezRecoveryAdapter("hci1", PHONE)
    props = {"Address": RADIO, "Powered": True, "Discovering": False,
             "Discoverable": False, "PowerState": "on"}
    objects = {adapter.path: {"org.bluez.Adapter1": props},
               adapter.device_path: {"org.bluez.Device1": {"Paired": True}}}
    fake = SimpleNamespace(props=props, objects=objects, writes=[], owner=":1.2", fail_off=False)

    def set_property(_interface, _name, value, **_kwargs):
        fake.writes.append(bool(value))
        props["Powered"] = bool(value)
        props["PowerState"] = "on" if value else "off"
        if not value and fake.fail_off:
            raise RuntimeError("reply lost after powering off")

    fake.GetManagedObjects = lambda **_kwargs: objects
    fake.GetAll = lambda *_args, **_kwargs: props
    fake.Get = lambda _interface, name, **_kwargs: props[name]
    fake.Set = set_property
    monkeypatch.setattr(mod, "get_system_bus", lambda: SimpleNamespace(
        get_name_owner=lambda *_: fake.owner,
        get_object=lambda *_: fake,
    ))
    monkeypatch.setattr(mod.dbus, "Interface", lambda obj, _interface: obj)
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


def test_timed_out_power_off_waits_for_transition_before_restoring(bluez, monkeypatch):
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

    def settle(_delay):
        fake.props.update(Powered=False, PowerState="off")

    fake.Set = delayed_off
    monkeypatch.setattr(mod.time, "sleep", settle)
    with pytest.raises(RuntimeError, match="off reply timed out"):
        adapter.cycle(before, threading.Event())
    assert fake.writes == [False, True]


def test_wait_for_power_transition_is_bounded(bluez, monkeypatch):
    adapter, fake = bluez
    fake.props["PowerState"] = "on-disabling"
    times = iter([100.0, 101.0, 131.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(mod.time, "sleep", lambda _delay: None)
    with pytest.raises(RuntimeError, match="transition did not finish"):
        adapter._settled_power(fake)
    assert not fake.writes
