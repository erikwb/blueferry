"""Race-resistant owner-only reads and atomic writes for small config files."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if path.is_symlink():
        raise PermissionError(f"private directory is a symlink: {path}")
    current = path.stat()
    if not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid():
        raise PermissionError(f"private directory has the wrong owner/type: {path}")
    if stat.S_IMODE(current.st_mode) != PRIVATE_DIRECTORY_MODE:
        path.chmod(PRIVATE_DIRECTORY_MODE)
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIRECTORY_MODE:
        raise PermissionError(f"could not secure private directory: {path}")


def runtime_private_directory() -> Path:
    """Return BlueFerry's owner-only volatile-data directory."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError(
            "temporary plaintext requires XDG_RUNTIME_DIR so it cannot be "
            "written to persistent storage"
        )
    root = Path(runtime_dir) / "blueferry"
    ensure_private_directory(root)
    return root


def create_runtime_private_file(*, prefix: str, suffix: str) -> tuple[int, Path]:
    """Create a 0600 file below the volatile-data directory."""
    descriptor, name = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=runtime_private_directory(),
    )
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    except Exception:
        os.close(descriptor)
        Path(name).unlink(missing_ok=True)
        raise
    return descriptor, Path(name)


def read_private_text(path: Path, *, maximum_bytes: int) -> str:
    """Read a regular owner-only file without following its final symlink."""
    if maximum_bytes < 1:
        raise ValueError("private file limit must be positive")
    if not path.parent.exists():
        raise FileNotFoundError(path)
    ensure_private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid():
            raise PermissionError(f"private file has the wrong owner/type: {path}")
        if stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE:
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        current = os.fstat(descriptor)
        if stat.S_IMODE(current.st_mode) != PRIVATE_FILE_MODE:
            raise PermissionError(f"could not secure private file: {path}")
        if current.st_size > maximum_bytes:
            raise ValueError(f"private file exceeds {maximum_bytes} bytes: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = stream.read(maximum_bytes + 1)
        if len(value.encode("utf-8")) > maximum_bytes:
            raise ValueError(f"private file exceeds {maximum_bytes} bytes: {path}")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_write_private_text(
    path: Path, value: str, *, maximum_bytes: int,
) -> None:
    encoded = value.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise ValueError(f"private file exceeds {maximum_bytes} bytes: {path}")
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
