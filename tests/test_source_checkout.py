from __future__ import annotations

from pathlib import Path

import blueferry


def test_suite_imports_the_checkout_not_an_installed_copy() -> None:
    root = Path(__file__).resolve().parents[1]
    package = Path(blueferry.__file__).resolve()

    assert package.is_relative_to(root / "src")
