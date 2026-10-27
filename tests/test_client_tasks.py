"""The toolkit-neutral timer over a schedule/cancel pair (no real GLib source)."""
from __future__ import annotations

from blueferry.client_tasks import ScheduledTimer


class _Loop:
    def __init__(self) -> None:
        self.sources: dict[int, tuple[int, object]] = {}
        self.removed: list[int] = []
        self._next = 0

    def schedule(self, delay, callback):
        self._next += 1
        self.sources[self._next] = (delay, callback)
        return self._next

    def cancel(self, source) -> None:
        self.removed.append(source)
        del self.sources[source]

    def fire(self, source) -> object:
        _delay, callback = self.sources.pop(source)
        return callback()


def test_restarting_replaces_the_pending_source_and_firing_forgets_it() -> None:
    loop = _Loop()
    fired: list[None] = []
    timer = ScheduledTimer(loop.schedule, loop.cancel, lambda: fired.append(None))
    timer.stop()  # nothing armed: nothing removed
    assert loop.removed == [] and not timer.active
    timer.start(150)
    timer.start(150)
    assert loop.removed == [1] and list(loop.sources) == [2] and timer.active
    assert loop.fire(2) is False  # a one-shot GLib source
    assert fired == [None] and not timer.active
    timer.stop()  # already fired: never removes a source that is gone
    assert loop.removed == [1]


def test_the_callback_may_rearm_the_timer() -> None:
    loop = _Loop()
    timer: ScheduledTimer

    def again() -> None:
        timer.start(5)

    timer = ScheduledTimer(loop.schedule, loop.cancel, again)
    timer.start(-3)
    assert loop.sources[1][0] == 0  # negative delays clamp to zero
    loop.fire(1)
    assert timer.active and list(loop.sources) == [2]
    timer.stop()
    assert loop.removed == [2] and loop.sources == {}
