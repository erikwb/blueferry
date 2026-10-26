"""The GTK window's own preferences: their own file, read and written on a worker."""
from __future__ import annotations

from blueferry.ui.ui_state import EXTRAS_EXPANDED, UiState
from tests.client_fakes import ManualRunner


def test_state_round_trips_through_its_own_file_on_the_worker(tmp_path) -> None:
    work = ManualRunner()
    path = tmp_path / "gtk-ui.json"
    state = UiState(work, path)
    loaded: list[dict] = []
    state.load(loaded.append)
    assert loaded == [] and not path.exists()  # nothing on the UI thread
    work.run_tasks()
    assert loaded == [{}]
    state.save(**{EXTRAS_EXPANDED: True})
    assert not path.exists()
    work.run_tasks()
    assert path.stat().st_mode & 0o077 == 0
    UiState(work, path).load(loaded.append)
    work.run_tasks()
    assert loaded[-1] == {EXTRAS_EXPANDED: True}


def test_an_unreadable_file_reads_as_empty(tmp_path) -> None:
    path = tmp_path / "gtk-ui.json"
    path.write_text("[not a dict")
    work = ManualRunner()
    loaded: list[dict] = []
    UiState(work, path).load(loaded.append)
    work.run_tasks()
    assert loaded == [{}]
