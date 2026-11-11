"""Loading gtk4-layer-shell before GTK, and pinning a window with it."""
from __future__ import annotations

import ctypes

import gi

from blueferry import gtk_layer_shell as ls


def test_preload_skips_without_wayland() -> None:
    calls: list[object] = []
    assert ls.preload({"GDK_BACKEND": "broadway", "WAYLAND_DISPLAY": "wayland-1"},
                      loader=calls.append) is None
    assert ls.preload({}, loader=calls.append) is None
    assert calls == []


def test_preload_prefers_env_path() -> None:
    seen: list[tuple[str, int]] = []
    env = {"WAYLAND_DISPLAY": "wayland-1", ls.ENV_LIBRARY: "/x/libgtk4-layer-shell.so.0"}
    loaded = ls.preload(env, loader=lambda path, mode: seen.append((path, mode)))
    assert loaded == "/x/libgtk4-layer-shell.so.0"
    assert seen == [("/x/libgtk4-layer-shell.so.0", ctypes.RTLD_GLOBAL)]


def test_preload_falls_back_to_soname_and_survives_missing_library() -> None:
    tried: list[str] = []

    def missing(path: str, mode: int) -> object:
        tried.append(path)
        raise OSError("not found")

    assert ls.preload({"WAYLAND_DISPLAY": "wayland-1", "GDK_BACKEND": "wayland"},
                      loader=missing) is None
    assert tried == [ls.LIBRARY_NAME]


def test_place_top_center_is_false_without_the_library(monkeypatch) -> None:
    def unavailable(namespace: str, version: str) -> None:
        if namespace == "Gtk4LayerShell":
            raise ValueError("Namespace Gtk4LayerShell not available")

    monkeypatch.setattr(gi, "require_version", unavailable)
    assert ls.place_top_center(object(), 40) is False
