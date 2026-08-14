"""Private build identity shared by clients, the daemon, and diagnostics."""

from __future__ import annotations

import re
from pathlib import Path

from blueferry.commands import run_command
from blueferry.errors import CommandError

BUILD_SHA_PATH = Path("/usr/share/blueferry/build-sha")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SHA = re.compile(r"[0-9a-f]{7,64}")


def _valid_sha(value: object) -> str | None:
    candidate = str(value or "").strip().casefold()
    return candidate if _SHA.fullmatch(candidate) else None


def installed_build_sha() -> str | None:
    """Return the content SHA baked into a native package."""
    try:
        return _valid_sha(BUILD_SHA_PATH.read_text(encoding="utf-8"))
    except OSError:
        return None


def source_git_sha(*, project_root: Path = _PROJECT_ROOT) -> str | None:
    """Return the checkout commit for source-run diagnostics."""
    try:
        result = run_command(
            ["git", "-C", str(project_root), "rev-parse", "--verify", "HEAD"],
            timeout=2,
        )
    except (CommandError, OSError):
        return None
    return _valid_sha(result.stdout)


def running_build_sha() -> str | None:
    """Return the packaged content SHA, or the checkout commit as a fallback."""
    return installed_build_sha() or source_git_sha()


def build_id(release: str, sha: str | None) -> str:
    """Combine a public release with a short private source identity."""
    valid = _valid_sha(sha)
    return f"{release}+sha.{valid[:12]}" if valid else release
