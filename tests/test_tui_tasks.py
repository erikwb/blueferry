"""The Textual TaskRunner/Timer adapters of the terminal client."""
from __future__ import annotations

import asyncio
import threading

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from blueferry.client_tasks import TaskRunner, Timer
from blueferry.tui_tasks import TextualTaskRunner, TextualTimer, timer_factory


class _App(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("tasks")


async def _until(pilot, predicate) -> None:
    for _attempt in range(60):
        if predicate():
            return
        await pilot.pause(0.05)
    assert predicate()


def test_the_adapters_satisfy_the_shared_protocols() -> None:
    app = _App()
    runner: TaskRunner = TextualTaskRunner(app)
    timer: Timer = TextualTimer(app, lambda: None)
    assert runner is not None and timer is not None


def test_results_arrive_on_the_ui_thread_done_before_finished() -> None:
    async def scenario() -> None:
        app = _App()
        events: list[tuple[str, object, bool]] = []
        worker_threads: list[int] = []
        async with app.run_test() as pilot:
            ui_thread = threading.get_ident()

            def operation() -> int:
                worker_threads.append(threading.get_ident())
                return 42

            def record(name: str):
                return lambda *args: events.append(
                    (name, args[0] if args else None, threading.get_ident() == ui_thread)
                )

            TextualTaskRunner(app).submit(
                operation, record("done"), record("error"), record("finished"),
            )
            await _until(pilot, lambda: len(events) == 2)
            assert worker_threads and worker_threads[0] != ui_thread
            assert events == [("done", 42, True), ("finished", None, True)]

    asyncio.run(scenario())


def test_a_failure_reports_its_message_then_finished() -> None:
    async def scenario() -> None:
        app = _App()
        events: list[tuple[str, object]] = []
        async with app.run_test() as pilot:
            def operation() -> None:
                raise RuntimeError("backend went away")

            TextualTaskRunner(app).submit(
                operation,
                lambda value: events.append(("done", value)),
                lambda message: events.append(("error", message)),
                lambda: events.append(("finished", None)),
            )
            await _until(pilot, lambda: len(events) == 2)
            assert events == [("error", "backend went away"), ("finished", None)]

    asyncio.run(scenario())


def test_callbacks_are_optional() -> None:
    async def scenario() -> None:
        app = _App()
        ran = threading.Event()
        async with app.run_test() as pilot:
            worker = TextualTaskRunner(app).submit(ran.set)
            await _until(pilot, lambda: ran.is_set() and worker.is_finished)
            worker = TextualTaskRunner(app).submit(lambda: 1 / 0)
            await _until(pilot, lambda: worker.is_finished)

    asyncio.run(scenario())


def test_a_removed_screen_gets_no_late_answer() -> None:
    async def scenario() -> None:
        app = _App()
        release = threading.Event()
        delivered: list[str] = []
        async with app.run_test() as pilot:
            screen = Screen()
            await app.push_screen(screen)
            worker = TextualTaskRunner(screen).submit(
                lambda: release.wait(5),
                lambda _value: delivered.append("done"),
                lambda _message: delivered.append("error"),
                lambda: delivered.append("finished"),
            )
            await app.pop_screen()
            await pilot.pause(0.05)
            release.set()
            await _until(pilot, lambda: worker.is_finished or worker.is_cancelled)
            await pilot.pause(0.1)
            assert delivered == []

    asyncio.run(scenario())


class _SlowUnmountScreen(Screen):
    """Hold the gap in which a closing screen has lost its children but not
    yet cancelled its workers (Unmount does that), and answer there."""

    def __init__(self, answer: threading.Event) -> None:
        super().__init__()
        self.answer = answer

    def compose(self) -> ComposeResult:
        yield Static("x", id="content")

    async def _dispatch_message(self, message):
        if isinstance(message, events.Unmount):
            self.answer.set()
            await asyncio.sleep(0.2)
        return await super()._dispatch_message(message)


@pytest.mark.parametrize("closing", ["pop", "quit"])
def test_an_answer_while_the_screen_closes_is_dropped(closing) -> None:
    async def scenario() -> None:
        app = _App()
        answer = threading.Event()
        screen = _SlowUnmountScreen(answer)
        delivered: list[str] = []

        def done(_value: object) -> None:
            screen.query_one("#content")  # raised NoMatches before the fix
            delivered.append("done")

        async with app.run_test() as pilot:
            await app.push_screen(screen)
            TextualTaskRunner(screen).submit(
                lambda: answer.wait(5), done,
                lambda _message: delivered.append("error"),
                lambda: delivered.append("finished"),
            )
            if closing == "pop":
                await app.pop_screen()
                await pilot.pause(0.4)
        assert answer.is_set() and delivered == []

    asyncio.run(scenario())


def test_timer_fires_once_restarts_and_stops() -> None:
    async def scenario() -> None:
        app = _App()
        fired: list[int] = []
        async with app.run_test() as pilot:
            timer = timer_factory(app)(lambda: fired.append(1))
            timer.start(30)
            assert timer.active
            await _until(pilot, lambda: fired == [1])
            assert not timer.active
            await pilot.pause(0.1)
            assert fired == [1]

            # A restart replaces the pending timeout instead of adding one.
            timer.start(60)
            timer.start(60)
            await _until(pilot, lambda: fired == [1, 1])
            await pilot.pause(0.15)
            assert fired == [1, 1]

            timer.start(30)
            timer.stop()
            timer.stop()
            assert not timer.active
            await pilot.pause(0.15)
            assert fired == [1, 1]

    asyncio.run(scenario())


def test_a_negative_delay_fires_at_once() -> None:
    async def scenario() -> None:
        app = _App()
        fired: list[int] = []
        async with app.run_test() as pilot:
            TextualTimer(app, lambda: fired.append(1)).start(-5)
            await _until(pilot, lambda: fired == [1])

    asyncio.run(scenario())
