"""Reading a plugin's standard log file in the clients (plugin API 1.4)."""
from __future__ import annotations

import os
import threading

import pytest
from typer.testing import CliRunner

from blueferry import plugin_logs
from blueferry.cli import app
from blueferry.plugin_api import logs as api_logs


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = api_logs.log_directory()
    directory.mkdir(parents=True)
    return directory


def test_tail_returns_the_last_lines_of_the_newest_candidate(state, tmp_path) -> None:
    assert plugin_logs.tail("io.example.ls") == (None, "")
    path = state / "io.example.ls.log"
    path.write_text("".join(f"line {n}\n" for n in range(500)))
    found, text = plugin_logs.tail("io.example.ls", 3)
    assert found == path and text == "line 497\nline 498\nline 499"
    # The bus daemon's environment may lack XDG_STATE_HOME: ~/.local/state too.
    other = tmp_path / "home" / ".local" / "state" / "blueferry" / "plugins"
    other.mkdir(parents=True)
    (other / "io.example.ls.log").write_text("from the default place\n")
    os.utime(path, (1, 1))
    assert plugin_logs.tail("io.example.ls")[1] == "from the default place"


def test_a_long_log_is_cut_at_a_line_start(state) -> None:
    path = state / "io.example.big.log"
    path.write_text("x" * (plugin_logs.MAX_TAIL_BYTES + 10) + "\nlast\n")
    assert plugin_logs.tail("io.example.big", 10)[1] == "last"


def test_symlinks_odd_ids_and_foreign_files_are_never_read(state, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not show")
    (state / "io.example.link.log").symlink_to(secret)
    assert plugin_logs.tail("io.example.link") == (None, "")
    assert plugin_logs.find_log("../../secret") is None
    with pytest.raises(OSError):
        plugin_logs.read_tail(state / "io.example.link.log")


def test_a_log_swapped_for_a_fifo_after_the_check_is_not_waited_on(state, monkeypatch) -> None:
    path = state / "io.example.ls.log"
    path.write_text("line\n")
    find_log = plugin_logs.find_log

    def find_then_swap(plugin_id):
        found = find_log(plugin_id)
        # Replaced by a FIFO after find_log looked at it, before it is read.
        found.unlink()
        os.mkfifo(found, 0o600)
        return found

    monkeypatch.setattr(plugin_logs, "find_log", find_then_swap)
    result = []
    reader = threading.Thread(
        target=lambda: result.append(plugin_logs.tail("io.example.ls")), daemon=True,
    )
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():
        # Release the blocked open so the hang fails this test only.
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=5)
        pytest.fail("reading a plugin log blocked on a FIFO until a writer appeared")
    assert result == [(None, "")]


def test_cli_plugins_log_prints_plain_text_or_the_path(state, tmp_path, monkeypatch) -> None:
    from blueferry import cli_plugins

    monkeypatch.setitem(cli_plugins._hooks, "manager", lambda: _NoManager())
    runner = CliRunner()
    missing = runner.invoke(app, ["plugins", "log", "io.example.ls"])
    assert missing.exit_code == 1 and "has not written a log yet" in missing.output
    path = state / "io.example.ls.log"
    path.write_text("one\nescape \x1b[2J here\nthree\n")
    shown = runner.invoke(app, ["plugins", "log", "io.example.ls", "-n", "2"])
    assert shown.exit_code == 0
    assert "escape" in shown.output and "\x1b" not in shown.output and "one" not in shown.output
    only = runner.invoke(app, ["plugins", "log", "io.example.ls", "--path"])
    assert only.output.strip() == str(path)


class _NoManager:
    def records(self):
        return {}
