"""Launch the TUI with a package-private dependency bundle when present."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PACKAGED_VENDOR = Path("/usr/lib/blueferry/vendor")
_PACKAGED_PYTHON_ROOT = Path("/usr/lib")
_LAUNCHER_PATH = Path(__file__)
_VENDORED_MODULE_ROOTS = frozenset({
    "linkify_it",
    "markdown_it",
    "mdit_py_plugins",
    "mdurl",
    "platformdirs",
    "pygments",
    "rich",
    "textual",
    "typing_extensions",
    "uc_micro",
})


def _vendor_is_active() -> bool:
    vendor = _PACKAGED_VENDOR.resolve()
    return any(Path(entry or ".").resolve() == vendor for entry in sys.path)


def _running_packaged_copy() -> bool:
    return _LAUNCHER_PATH.resolve().is_relative_to(_PACKAGED_PYTHON_ROOT)


def _drop_shadowed_modules() -> None:
    """Forget distro modules that the private Textual bundle replaces."""
    for name in tuple(sys.modules):
        if name.partition(".")[0] in _VENDORED_MODULE_ROOTS:
            sys.modules.pop(name, None)


def main() -> int:
    """Run the TUI, adding the private DEB/RPM dependency path if present."""
    if (
        _running_packaged_copy()
        and _PACKAGED_VENDOR.is_dir()
        and not _vendor_is_active()
    ):
        sys.path.insert(0, str(_PACKAGED_VENDOR))
        _drop_shadowed_modules()
        importlib.invalidate_caches()

    try:
        from blueferry.tui import main as tui_main
    except ModuleNotFoundError as error:
        if error.name != "blueferry.tui":
            raise
        print(
            "The BlueFerry TUI is not installed; reinstall blueferry-backend.",
            file=sys.stderr,
        )
        return 2

    return tui_main()


if __name__ == "__main__":
    raise SystemExit(main())
