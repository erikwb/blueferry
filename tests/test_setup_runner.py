from __future__ import annotations

import threading

from blueferry.ui.setup_runner import SetupRunner


def test_setup_runner_hands_success_back_to_caller() -> None:
    completed = threading.Event()
    values = []

    def handoff(callback, value):
        callback(value)

    SetupRunner(handoff).run(
        lambda: 42,
        lambda value: (values.append(value), completed.set()),
        lambda error: values.append(error),
    )

    assert completed.wait(1)
    assert values == [42]


def test_setup_runner_normalizes_failure_text() -> None:
    completed = threading.Event()
    errors = []

    def handoff(callback, value):
        callback(value)

    SetupRunner(handoff).run(
        lambda: (_ for _ in ()).throw(RuntimeError("failed")),
        lambda _value: None,
        lambda error: (errors.append(error), completed.set()),
    )

    assert completed.wait(1)
    assert errors == ["failed"]
