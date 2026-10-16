"""Plugin API 1.4: the standard plugin log file (plugin_api.logs)."""
from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from blueferry.plugin_api import logs as plugin_logs


def test_log_path_follows_xdg_state_home_and_rejects_odd_ids(tmp_path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    assert plugin_logs.log_path("io.example.x", env) == (
        tmp_path / "state" / "blueferry" / "plugins" / "io.example.x.log")
    relative = plugin_logs.log_path("io.example.x", {"XDG_STATE_HOME": "relative"})
    assert relative == Path(os.path.expanduser("~")) / ".local" / "state" / "blueferry" / (
        "plugins") / "io.example.x.log"
    paths = plugin_logs.candidate_paths("io.example.x", env)
    assert len(paths) == 2 and paths[1].parts[-5:-2] == (".local", "state", "blueferry")
    for bad in ("", "../x", "a/b", ".hidden", "x..y", "a" * 200):
        with pytest.raises(ValueError):
            plugin_logs.log_path(bad, env)


@pytest.mark.parametrize(("line", "hidden"), [
    ("Authorization: Bearer abcdefgh1234", "abcdefgh1234"),
    ("GET /upload?sessionId=s3cr3t&fileId=1&token=t0k3n", "t0k3n"),
    ('{"password": "hunter22", "user": "anna"}', "hunter22"),
    ("pin=123456 rejected", "123456"),
    ("api_key: AbCdEf123456", "AbCdEf123456"),
    ("basic   QWxhZGRpbjpvcGVu", "QWxhZGRpbjpvcGVu"),
])
def test_redact_masks_secret_values(line, hidden) -> None:
    redacted = plugin_logs.redact(line)
    assert hidden not in redacted and "***" in redacted


def test_redact_leaves_ordinary_words() -> None:
    line = "3 tokens issued, pinned certificate, password change screen"
    assert plugin_logs.redact(line) == line


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hook, thread_hook = sys.excepthook, threading.excepthook
    yield root
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hook, thread_hook
    logging.captureWarnings(False)


def test_setup_logging_writes_a_private_rotating_redacted_file(tmp_path, clean_root_logger):
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    path = plugin_logs.setup_logging("io.example.x", environ=env, max_bytes=400, backups=1)
    assert path == plugin_logs.log_path("io.example.x", env)
    assert plugin_logs.setup_logging("io.example.x", environ=env) == path  # idempotent
    assert sum(getattr(h, "blueferry_plugin_id", "") == "io.example.x"
               for h in clean_root_logger.handlers) == 1
    logger = logging.getLogger("io.example.x.test")
    logger.info("hello token=abc123")
    logger.debug("debug lines stay out at INFO")
    try:
        raise RuntimeError("boom password=pw1")
    except RuntimeError:
        logger.exception("failed")
    text = path.read_text()
    assert "hello token=***" in text and "abc123" not in text
    assert "debug lines" not in text
    assert "RuntimeError" in text and "pw1" not in text
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    for _ in range(20):
        logger.info("filler line to rotate the file")
    assert path.with_name(path.name + ".1").exists()
    assert not path.with_name(path.name + ".2").exists()


def test_setup_logging_records_uncaught_thread_errors(tmp_path, clean_root_logger) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}
    path = plugin_logs.setup_logging("io.example.y", environ=env)
    worker = threading.Thread(target=lambda: 1 / 0, name="worker-x")
    worker.start()
    worker.join()
    assert "uncaught exception in thread worker-x" in path.read_text()
    assert "ZeroDivisionError" in path.read_text()


def test_setup_logging_refuses_a_symlinked_file_and_never_raises(
    tmp_path, clean_root_logger, capsys,
) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}
    directory = plugin_logs.log_directory(env)
    directory.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("")
    (directory / "io.example.z.log").symlink_to(target)
    assert plugin_logs.setup_logging("io.example.z", environ=env) is None
    assert "log unavailable" in capsys.readouterr().err
    assert target.read_text() == ""


def test_level_comes_from_the_environment(tmp_path, clean_root_logger) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path), plugin_logs.LEVEL_VARIABLE: "debug"}
    path = plugin_logs.setup_logging("io.example.d", environ=env)
    logging.getLogger("io.example.d").debug("now visible")
    assert "now visible" in path.read_text()


def test_a_symlinked_blueferry_directory_is_refused(tmp_path, clean_root_logger) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    (tmp_path / "state").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "state" / "blueferry").symlink_to(tmp_path / "elsewhere")
    assert plugin_logs.setup_logging("io.example.s", environ=env) is None
    assert not any((tmp_path / "elsewhere").iterdir())


def test_other_handlers_keep_their_threshold(tmp_path, clean_root_logger) -> None:
    import io

    stream = io.StringIO()
    other = logging.StreamHandler(stream)
    clean_root_logger.addHandler(other)
    clean_root_logger.setLevel(logging.WARNING)
    path = plugin_logs.setup_logging("io.example.t", environ={"XDG_STATE_HOME": str(tmp_path)})
    logging.getLogger("io.example.t").info("only in the file")
    assert "only in the file" in path.read_text() and stream.getvalue() == ""
    assert stat.S_IMODE(os.stat(path.parent.parent).st_mode) == 0o700
