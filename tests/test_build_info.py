from __future__ import annotations

from blueferry import build_info


def test_installed_build_sha_is_validated(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "build-sha"
    monkeypatch.setattr(build_info, "BUILD_SHA_PATH", marker)
    marker.write_text("A" * 64 + "\n", encoding="utf-8")

    assert build_info.installed_build_sha() == "a" * 64

    marker.write_text("not-a-sha\n", encoding="utf-8")
    assert build_info.installed_build_sha() is None


def test_build_id_uses_a_short_private_sha() -> None:
    assert build_info.build_id("0.6.3-1", "b" * 64) == (
        "0.6.3-1+sha." + "b" * 12
    )
    assert build_info.build_id("0.6.3", None) == "0.6.3"
