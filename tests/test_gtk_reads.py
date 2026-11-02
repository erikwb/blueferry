"""Coalesced reads of the GTK phone card and Calls tab (no toolkit)."""
from __future__ import annotations

from blueferry.ui.reads import CoalescedRead


def _read(active=lambda: True):
    requests, loaded, failed = [], [], []
    read = CoalescedRead(
        lambda ok, err: requests.append((ok, err)), loaded.append, failed.append,
        active=active,
    )
    return read, requests, loaded, failed


def test_a_burst_of_invalidations_reads_once_and_then_once_more():
    read, requests, loaded, _failed = _read()
    for _ in range(1000):
        read.refresh()
    assert len(requests) == 1 and read.pending
    requests.pop()[0]("first")
    assert loaded == ["first"] and len(requests) == 1  # the latest state, once
    requests.pop()[0]("second")
    assert loaded == ["first", "second"] and requests == [] and not read.pending


def test_failures_are_reported_and_end_the_read():
    read, requests, loaded, failed = _read()
    read.refresh()
    requests.pop()[1]("Timed out")
    assert failed == ["Timed out"] and loaded == [] and not read.pending


def test_reset_drops_replies_of_earlier_reads():
    read, requests, loaded, failed = _read()
    read.refresh()
    read.refresh()  # would re-read after the reply
    read.reset()
    requests.pop()[0]("stale")
    assert loaded == [] and failed == [] and requests == [] and not read.pending
    read.refresh()
    requests.pop()[0]("fresh")
    assert loaded == ["fresh"]


def test_nothing_is_read_or_delivered_while_inactive():
    active = [False]
    read, requests, loaded, _failed = _read(lambda: active[0])
    read.refresh()
    assert requests == []
    active[0] = True
    read.refresh()
    active[0] = False  # e.g. the opt-in was turned off meanwhile
    requests.pop()[0]("private")
    assert loaded == []
