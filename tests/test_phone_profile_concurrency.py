"""Profile migration cannot clobber a separate pairing helper's update."""

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from blueferry import config
from blueferry.private_files import atomic_write_private_text

_PAIRING_HELPER = """
import fcntl
import sys
from pathlib import Path
from blueferry import config, pair_setup

path = Path(sys.argv[1])
action = sys.argv[2]
config.LOCAL_ENV_PATH = pair_setup.LOCAL_ENV_PATH = path
config.SETTINGS_JSON = path.parent / "settings.json"
original_flock = fcntl.flock
original_read = config.read_local_env

def observed_flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            original_flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            print("waiting", flush=True)
            original_flock(fd, operation)
    else:
        original_flock(fd, operation)

def observed_read(*args, **kwargs):
    print("read", flush=True)
    return original_read(*args, **kwargs)

fcntl.flock = observed_flock
config.read_local_env = observed_read
if action == "forget":
    pair_setup.clear_local_target()
else:
    mac = "02:00:00:00:00:02" if action == "new_phone" else "02:00:00:00:00:01"
    pair_setup.write_local_env(mac, "hci1", ancs_enabled=False)
print("done", flush=True)
"""


@pytest.mark.parametrize("action", ["same_phone", "new_phone", "forget"])
def test_pairing_helper_waits_for_migration_before_reading_and_writing(
    isolated_state, monkeypatch, action,
):
    target = config.LOCAL_ENV_PATH
    atomic_write_private_text(
        target,
        "BLUEFERRY_MAC=02:00:00:00:00:01\nBLUEFERRY_HISTORY_RETENTION_DAYS=7\n",
        maximum_bytes=config.MAX_CONFIG_FILE_BYTES,
    )
    helpers = []

    def write_with_competing_pairing_helper(path, content, **kwargs):
        # Pause the migration after reading its snapshot and before replacing
        # the file, precisely where an unlocked helper could lose its update.
        helper = subprocess.Popen(
            [sys.executable, "-c", _PAIRING_HELPER, str(target), action],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        helpers.append(helper)
        assert select.select([helper.stdout], [], [], 10)[0], "pairing helper did not respond"
        # It must block on the migration lock before reading any configuration.
        assert helper.stdout.readline().strip() == "waiting"
        atomic_write_private_text(path, content, **kwargs)

    monkeypatch.setattr(config, "atomic_write_private_text", write_with_competing_pairing_helper)
    try:
        migrated_id = config.current_profile_id("02:00:00:00:00:01")
        assert migrated_id is not None
        assert len(helpers) == 1
        output, errors = helpers[0].communicate(timeout=10)
        assert helpers[0].returncode == 0, errors
        assert output.splitlines() == ["read", "done"]
    finally:
        for helper in helpers:
            if helper.poll() is None:
                helper.kill()
            helper.communicate(timeout=10)

    saved = config.read_local_env()
    assert saved["BLUEFERRY_HISTORY_RETENTION_DAYS"] == "7"
    if action == "forget":
        assert saved == {"BLUEFERRY_HISTORY_RETENTION_DAYS": "7"}
    else:
        assert saved["BLUEFERRY_ADAPTER"] == "hci1"
        assert saved["BLUEFERRY_ANCS_ENABLED"] == "false"
        if action == "same_phone":
            assert saved["BLUEFERRY_MAC"] == "02:00:00:00:00:01"
            assert saved[config.PROFILE_ID_KEY] == migrated_id
        else:
            assert saved["BLUEFERRY_MAC"] == "02:00:00:00:00:02"
            assert saved[config.PROFILE_ID_KEY] != migrated_id
