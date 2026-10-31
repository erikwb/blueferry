"""The dbus-python CardChanged watcher of the terminal client and the
Quickshell bridge (a fake bus only)."""
from __future__ import annotations

from blueferry.plugin_card_watch import CardChangeWatcher


class _Bus:
    def __init__(self) -> None:
        self.receivers: dict[str, object] = {}
        self.matches: list[dict] = []
        self.removed: list[str] = []

    def add_signal_receiver(self, handler, **match):
        bus = self
        name = match["bus_name"]
        self.matches.append(match)
        self.receivers[name] = handler

        class _Match:
            def remove(self) -> None:
                bus.removed.append(name)

        return _Match()


def test_watcher_follows_the_named_plugins_only() -> None:
    bus = _Bus()
    changes: list[None] = []
    watcher = CardChangeWatcher(lambda: changes.append(None), lambda: bus)
    watcher.watch(["io.weirdware.BlueFerry.Plugin.a", "io.weirdware.BlueFerry.Plugin.b", ""])
    assert watcher.names == ["io.weirdware.BlueFerry.Plugin.a", "io.weirdware.BlueFerry.Plugin.b"]
    assert {(m["signal_name"], m["dbus_interface"], m["path"]) for m in bus.matches} == {
        ("CardChanged", "io.weirdware.BlueFerry.Plugin1", "/io/weirdware/BlueFerry/Plugin")}
    watcher.watch(["io.weirdware.BlueFerry.Plugin.b"])
    assert bus.removed == ["io.weirdware.BlueFerry.Plugin.a"]
    bus.receivers["io.weirdware.BlueFerry.Plugin.b"]()
    assert changes == [None]
    watcher.stop()
    assert watcher.names == [] and bus.removed[-1] == "io.weirdware.BlueFerry.Plugin.b"


def test_a_missing_session_bus_watches_nothing() -> None:
    def no_bus():
        raise RuntimeError("no session bus")

    watcher = CardChangeWatcher(lambda: None, no_bus)
    watcher.watch(["io.weirdware.BlueFerry.Plugin.a"])
    assert watcher.names == []
