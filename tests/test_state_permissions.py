"""Private message and contact state remains owner-only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blueferry import config
from blueferry.history import read_events
from blueferry.sinks.sqlite import SqliteSink


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    state_dir = tmp_path / "blueferry"
    events = state_dir / "events.sqlite"
    contacts = state_dir / "contacts.sqlite"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "EVENTS_DB", events)
    monkeypatch.setattr(config, "CONTACTS_DB", contacts)
    return state_dir, events


def _mode(path):
    return path.stat().st_mode & 0o777


def test_state_dir_is_owner_only(state_paths) -> None:
    state_dir, _events = state_paths
    config.ensure_dirs()
    assert _mode(state_dir) == 0o700


def test_existing_world_readable_state_is_repaired(state_paths) -> None:
    state_dir, events = state_paths
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o755)
    events.write_text("{}\n")
    events.chmod(0o644)

    config.ensure_dirs()

    assert _mode(state_dir) == 0o700
    assert _mode(events) == 0o600


def test_open_state_file_creates_owner_only_file(state_paths) -> None:
    _state_dir, events = state_paths
    config.ensure_dirs()
    with config.open_state_file(events, "a") as stream:
        stream.write("{}\n")
    assert _mode(events) == 0o600


def test_sqlite_sink_preserves_private_message_content(state_paths) -> None:
    _state_dir, events = state_paths
    sink = SqliteSink(path=events)
    sink._append({"kind": "sms_received", "body": "private"})

    assert _mode(events) == 0o600
    assert read_events(path=events)[0]["body"] == "private"


def test_sqlite_sink_refuses_non_messages_ancs_content(state_paths) -> None:
    _state_dir, events = state_paths
    sink = SqliteSink(path=events)
    event = SimpleNamespace(
        app_id="com.example.Private",
        correlation_dict=lambda: {"body": "must not be called"},
    )

    sink.handle_ancs(event)

    assert read_events(path=events) == []


def test_symlinked_state_directory_is_rejected(tmp_path, monkeypatch) -> None:
    target = tmp_path / "shared"
    target.mkdir()
    state_dir = tmp_path / "blueferry"
    state_dir.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "EVENTS_DB", state_dir / "events.sqlite")
    monkeypatch.setattr(config, "CONTACTS_DB", state_dir / "contacts.sqlite")

    with pytest.raises(PermissionError, match="symlink"):
        config.ensure_dirs()
