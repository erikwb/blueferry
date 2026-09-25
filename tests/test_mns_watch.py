"""Inert tests for the MNS connection watch; the bus and timers are fakes."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

from blueferry.obex import mns_watch

PHONE = "28:D5:B1:03:0E:4C"
MNS_PATH = "/org/bluez/obex/server/session3"


def _mns(destination: str = PHONE, target: str = mns_watch.MNS_TARGET) -> dict:
    return {mns_watch.SESSION_INTERFACE: {"Destination": destination, "Target": target}}


class _Harness:
    def __init__(self, monkeypatch, existing: dict | None = None) -> None:
        self.handlers: dict[str, object] = {}
        self.removed_matches: list[str] = []
        self.timers: dict[int, tuple[int, object]] = {}
        self.missing: list[str] = []
        self.present: list[bool] = []
        ids = itertools.count(1)

        def subscribe(handler, *, signal_name, **_kwargs):
            self.handlers[signal_name] = handler
            return SimpleNamespace(remove=lambda: self.removed_matches.append(signal_name))

        bus = SimpleNamespace(add_signal_receiver=subscribe, get_object=lambda *_a: object())
        monkeypatch.setattr(mns_watch, "get_session_bus", lambda: bus)
        monkeypatch.setattr(
            mns_watch.dbus,
            "Interface",
            lambda *_a: SimpleNamespace(GetManagedObjects=lambda **_k: existing or {}),
        )

        def schedule(delay, callback):
            timer = next(ids)
            self.timers[timer] = (delay, callback)
            return timer

        self.watch = mns_watch.MnsWatch(
            PHONE.lower(),
            on_missing=self.missing.append,
            on_present=lambda: self.present.append(True),
            schedule=schedule,
            cancel=lambda timer: self.timers.pop(timer, None),
        )

    def added(self, path: str, interfaces: dict) -> None:
        self.handlers["InterfacesAdded"](path, interfaces)

    def removed(self, path: str) -> None:
        self.handlers["InterfacesRemoved"](path, [mns_watch.SESSION_INTERFACE])

    def fire(self) -> None:
        (timer,) = self.timers
        _delay, callback = self.timers.pop(timer)
        callback()

    def delays(self) -> list[int]:
        return [delay for delay, _callback in self.timers.values()]


def test_an_mns_session_that_already_exists_counts_as_connected(monkeypatch):
    harness = _Harness(monkeypatch, existing={MNS_PATH: _mns()})

    harness.watch.start()

    assert harness.watch.connected
    assert harness.present == [True]
    assert harness.timers == {}


def test_missing_mns_is_reported_once_the_open_grace_expires(monkeypatch):
    harness = _Harness(monkeypatch)
    harness.watch.start()
    assert harness.delays() == [mns_watch.OPEN_GRACE_SECONDS]

    harness.fire()

    assert harness.missing == ["the iPhone did not open MAP notifications"]


def test_mns_opening_during_the_grace_cancels_the_report(monkeypatch):
    harness = _Harness(monkeypatch)
    harness.watch.start()

    harness.added(MNS_PATH, _mns())

    assert harness.timers == {}
    assert harness.present == [True]


def test_other_sessions_do_not_count_as_mns(monkeypatch):
    harness = _Harness(monkeypatch)
    harness.watch.start()

    harness.added("/org/bluez/obex/server/session4", _mns(destination="AA:BB:CC:DD:EE:FF"))
    harness.added("/org/bluez/obex/server/session5", _mns(target="00001105-0000-1000-8000-00805F9B34FB"))
    harness.added("/org/bluez/obex/client/session6", _mns())

    assert not harness.watch.connected
    assert harness.delays() == [mns_watch.OPEN_GRACE_SECONDS]


def test_losing_mns_is_reported_after_the_loss_grace(monkeypatch):
    harness = _Harness(monkeypatch, existing={MNS_PATH: _mns()})
    harness.watch.start()

    harness.removed(MNS_PATH)

    assert harness.delays() == [mns_watch.LOSS_GRACE_SECONDS]
    harness.fire()
    assert harness.missing == ["the iPhone closed MAP notifications"]


def test_mns_returning_within_the_loss_grace_is_not_reported(monkeypatch):
    harness = _Harness(monkeypatch, existing={MNS_PATH: _mns()})
    harness.watch.start()
    harness.removed(MNS_PATH)

    harness.added("/org/bluez/obex/server/session9", _mns())

    assert harness.timers == {}
    assert harness.missing == []


def test_stopping_cancels_reports_and_signal_watches(monkeypatch):
    harness = _Harness(monkeypatch)
    harness.watch.start()

    harness.watch.stop()
    harness.added(MNS_PATH, _mns())

    assert harness.timers == {}
    assert harness.present == []
    assert sorted(harness.removed_matches) == ["InterfacesAdded", "InterfacesRemoved"]
