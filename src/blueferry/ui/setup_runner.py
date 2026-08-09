"""GTK-independent worker used for blocking first-run setup operations."""
from __future__ import annotations

import threading
from collections.abc import Callable


class SetupRunner:
    """Run setup work once and marshal its outcome through a supplied handoff."""

    def __init__(self, handoff: Callable[..., object]) -> None:
        self._handoff = handoff

    def run(self, operation, on_success, on_failure) -> None:
        def work() -> None:
            try:
                value = operation()
            except Exception as error:
                self._handoff(on_failure, str(error))
                return
            self._handoff(on_success, value)

        threading.Thread(
            target=work,
            name="blueferry-setup",
            daemon=True,
        ).start()
