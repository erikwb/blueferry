"""Keep a settings switch on the user's choice while its save settles.

A switch that saves through the daemon sees two kinds of status afterwards:
one requested before the save finished, which still reports the old value,
and the ones after it. The first must not flip the switch back.
"""
from __future__ import annotations


class SavedChoice:
    def __init__(self) -> None:
        # The choice being saved, and the saved choice until a status
        # confirms it.
        self._pending: bool | None = None
        self._confirmed: bool | None = None

    @property
    def saving(self) -> bool:
        return self._pending is not None

    def begin(self, choice: bool) -> None:
        self._pending = choice
        self._confirmed = None

    def saved(self, choice: bool) -> None:
        self._pending = None
        self._confirmed = choice

    def failed(self) -> None:
        self._pending = None

    def resolve(self, reported: bool) -> tuple[bool, bool]:
        """Return the position to show for a status reporting ``reported``.

        The second value is True when that status was read before the save
        finished. It is skipped once; the caller should ask again, so a
        change made elsewhere in the meantime still shows up.
        """
        if self._pending is not None:
            return self._pending, False
        confirmed, self._confirmed = self._confirmed, None
        if confirmed is None or reported == confirmed:
            return reported, False
        return confirmed, True
