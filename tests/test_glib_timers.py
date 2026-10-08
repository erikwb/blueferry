"""schedule_periodic forgets a source id exactly when GLib destroys it."""
from __future__ import annotations

import pytest

from blueferry.glib_timers import schedule_periodic


def _arm(callback):
    ticks = []
    forgotten = []

    def schedule(seconds, tick):
        ticks.append((seconds, tick))
        return 7

    assert schedule_periodic(schedule, 30, callback, lambda: forgotten.append(True)) == 7
    ((seconds, tick),) = ticks
    assert seconds == 30
    return tick, forgotten


@pytest.mark.parametrize("result", [True, 1, "yes"])
def test_a_continuing_callback_keeps_its_id(result):
    tick, forgotten = _arm(lambda: result)

    assert tick() is True
    assert forgotten == []


@pytest.mark.parametrize("result", [False, None, 0])
def test_a_stopping_callback_forgets_its_id(result):
    tick, forgotten = _arm(lambda: result)

    assert tick() is False
    assert forgotten == [True]


def test_a_raising_callback_forgets_its_id_and_still_raises():
    def fail():
        raise OSError("half-written marker")

    tick, forgotten = _arm(fail)

    with pytest.raises(OSError, match="half-written marker"):
        tick()
    assert forgotten == [True]
