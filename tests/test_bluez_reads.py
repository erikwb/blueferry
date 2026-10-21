"""A wedged BlueZ must not stall periodic checks or provide stale evidence."""
from types import SimpleNamespace

import dbus
import pytest

from blueferry.bluez_reads import BluezReadCache


@pytest.fixture
def reads():
    calls = []
    now = [0.0]

    def call_async(*args, **kwargs):
        assert kwargs == {"timeout": 5.0}
        calls.append(args)

    cache = BluezReadCache(lambda: SimpleNamespace(call_async=call_async), lambda: now[0])
    return cache, calls, now


def pending(read):
    with pytest.raises(dbus.exceptions.DBusException) as raised:
        read()
    assert raised.value.get_dbus_name() == "org.freedesktop.DBus.Error.NoReply"


def test_unanswered_reads_return_immediately_and_coalesce(reads):
    cache, calls, now = reads
    def read():
        return cache.property("/device", "org.bluez.Bearer.LE1", "Connected")
    for now[0] in range(20):
        pending(read)
    assert len(calls) == 1
    assert calls[0][:6] == ("org.bluez", "/device", "org.freedesktop.DBus.Properties",
                           "Get", "ss", ("org.bluez.Bearer.LE1", "Connected"))
    calls[0][-2](True)
    assert read() is True
    now[0] += 5
    assert read() is True
    assert len(calls) == 2
    now[0] += 6
    pending(read)  # Cached Connected=true has expired even without a callback.
    assert len(calls) == 2


def test_error_replaces_cached_success_and_retries_are_bounded(reads):
    cache, calls, now = reads
    def read():
        return cache.property("/device", "org.bluez.Bearer.LE1", "Connected")
    pending(read)
    calls[0][-2](True)
    now[0] = 5
    assert read() is True
    calls[-1][-1](dbus.exceptions.DBusException("timeout", name="org.freedesktop.DBus.Error.NoReply"))
    pending(read)
    assert len(calls) == 2
    now[0] = 10
    pending(read)
    assert len(calls) == 3
    calls[-1][-2](False)
    assert read() is False


def test_property_signal_beats_old_reply_and_invalidations_expire_value(reads):
    cache, calls, _now = reads
    def read():
        return cache.property("/device", "org.bluez.Bearer.LE1", "Connected")
    pending(read)
    cache.properties_changed("/device", "org.bluez.Bearer.LE1", {"Connected": False}, [])
    calls[0][-2](True)
    assert read() is False
    cache.properties_changed("/device", "org.bluez.Bearer.LE1", {}, ["Connected"])
    pending(read)
    calls[-1][-2](True)
    assert read() is True


def test_five_second_poll_does_not_expire_prompt_successful_replies(reads):
    cache, calls, now = reads

    def read():
        return cache.property("/device", "org.bluez.Bearer.LE1", "Connected")

    pending(read)
    now[0] = 0.0001
    calls[-1][-2](True)
    for tick in range(1, 30):
        # A reply lands just after one poll; ordinary timer jitter makes
        # the following poll slightly less than five seconds after it.
        now[0] = tick * 5 + (tick // 2) * 0.001
        assert read() is True
        now[0] += 0.0001
        calls[-1][-2](True)


@pytest.mark.parametrize("invalidation", ["invalidate", "invalidate_managed_objects"])
def test_late_snapshot_cannot_survive_owner_or_topology_change(reads, invalidation):
    cache, calls, _now = reads
    def read():
        return cache.read("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", "", (), owner=":1.2")
    pending(read)
    getattr(cache, invalidation)()
    pending(read)
    calls[0][-2]({"stale": True})
    pending(read)
    calls[-1][-2]({"fresh": True})
    assert read() == {"fresh": True}


def test_periodic_bond_check_distinguishes_unknown_from_removed(reads, monkeypatch):
    from blueferry import pair_setup

    cache, calls, now = reads
    monkeypatch.setattr(pair_setup, "observations", cache)
    monkeypatch.setattr(pair_setup, "_object_manager", lambda: pytest.fail("synchronous bond read"))
    adapter = "/org/bluez/hci0"
    device = adapter + "/dev_AA_BB_CC_DD_EE_FF"

    def bonded():
        return pair_setup.bond_status("AA:BB:CC:DD:EE:FF", "hci0", cached=True)

    assert bonded() is None
    calls[-1][-2]({adapter: {"org.bluez.Adapter1": {}},
                  device: {"org.bluez.Device1": {"Paired": True}}})
    assert bonded() is True
    now[0] = 11
    assert bonded() is None  # A wedged BlueZ is not an explicit unpair.
    calls[-1][-2]({})
    assert bonded() is None  # Missing controller is also transient.
    cache.invalidate_managed_objects()
    assert bonded() is None
    calls[-1][-2]({adapter: {"org.bluez.Adapter1": {}}})
    assert bonded() is False  # Controller present, saved phone gone.
