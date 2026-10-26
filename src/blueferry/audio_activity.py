"""Serialize pairing and audio changes across the setup helper and daemon."""
from __future__ import annotations

from contextlib import contextmanager

from blueferry import config
from blueferry.private_files import private_file_lock


@contextmanager
def audio_activity(*, blocking: bool = True):
    # A permanent sidecar is locked, never a replaceable settings inode. The
    # kernel releases it if a pairing helper crashes or is cancelled.
    with private_file_lock(config.CONFIG_DIR / "audio-activity", blocking=blocking):
        yield
