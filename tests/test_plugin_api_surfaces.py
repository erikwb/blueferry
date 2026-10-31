"""Plugin API 1.2: card items, share targets and popups (both sides)."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blueferry.plugin_api import API_MINOR, KNOWN_CAPABILITIES
from blueferry.plugin_api import surfaces as sf
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.manifest import ManifestError
from blueferry.plugin_api.service import (
    CardService,
    NotifyService,
    PluginCallError,
    ShareService,
    emit_card_changed,
    emit_notify,
)
from blueferry.plugin_api.testing import FakeHost, ScriptedTransport, inline_service, manifest


def _manifest(caps: str = "card;share;notify;", version: str = "1.2"):
    return manifest("io.example.surfaces", capabilities=caps, api_version=version)


@pytest.mark.parametrize("kind", [[], {}])
def test_malformed_action_kind_uses_button_fallback(kind):
    items = sf.parse_card_items(json.dumps({"items": [{
        "id": "item", "title": "Title", "actions": [
            {"id": "action", "label": "Action", "kind": kind},
        ],
    }]}))
    assert items[0].actions[0].kind == "button"


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "cache" / "blueferry"
    (root / "plugin").mkdir(parents=True)
    (root / "io.example.surfaces").mkdir()
    return root


class _Everything(CardService, ShareService, NotifyService):
    def __init__(self, *args, cache=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache = cache
        self.sent: list[tuple[str, list[str]]] = []
        self.clicked: list[tuple[str, str, dict]] = []

    def card_items(self):
        return [
            sf.CardItem("next", "Dentist  \x1b[31m10:00", icon="view-calendar",
                        subtitle="in 20 min", actions=[
                            sf.Action("open", "Open", kind="primary"),
                            sf.Action("snooze", "Snooze", icon="alarm-symbolic"),
                        ]),
            {"id": "raw", "title": "Plain dict works too", "actions": []},
        ]

    def invoke_action(self, item_id, action_id, args):
        self.clicked.append((item_id, action_id, args))
        if action_id == "open" and self.cache is not None:
            target = self.cache / "io.example.surfaces" / "event.ics"
            target.write_text("x")
            return sf.ActionResult(True, "Opened", target.as_uri())
        if action_id == "web":
            return {"ok": True, "message": None, "open_uri": "https://example.org/x"}
        if action_id == "evil":
            return {"ok": True, "open_uri": "file:///etc/passwd"}
        return sf.ActionResult(True, f"{action_id} done")

    def share_targets(self):
        return [sf.ShareTarget("phone", "iPhone (LocalSend)", "smartphone")]

    def send_files(self, target_id, paths):
        self.sent.append((target_id, paths))
        return sf.SendResult(True, "Sending 1 file", "job-1")


def test_capabilities_and_minor_version_are_known() -> None:
    assert {"card", "share", "notify"} <= KNOWN_CAPABILITIES
    assert API_MINOR == 6
    assert _manifest().api_minor == 2


def test_a_manifest_needing_a_newer_minor_is_ignored_with_a_reason() -> None:
    with pytest.raises(ManifestError, match=r"needs plugin API 1\.7; this BlueFerry supports 1\.6"):
        _manifest(version="1.7")


def test_value_types_reject_bad_ids_early() -> None:
    with pytest.raises(ValueError):
        sf.CardItem("../x", "Title")
    with pytest.raises(ValueError):
        sf.Action("ok", "Label", kind="danger")
    with pytest.raises(ValueError):
        sf.CardItem("a", "T", actions=[sf.Action(f"a{i}", "x") for i in range(4)])


def test_fake_host_round_trip_for_card_share_and_notify(cache, tmp_path) -> None:
    service = inline_service(_Everything, _manifest(), cache=cache)
    host = FakeHost(service, cache_root=cache)

    items = host.card_items()
    assert [item.id for item in items] == ["next", "raw"]
    assert items[0].title == "Dentist [31m10:00" and items[0].icon == "view-calendar"
    assert [action.kind for action in items[0].actions] == ["primary", "button"]

    opened = host.invoke("next", "open", {"when": "now"})
    assert opened.ok and opened.open_uri == (
        cache / "io.example.surfaces" / "event.ics").resolve().as_uri()
    assert service.clicked[-1] == ("next", "open", {"when": "now"})
    assert host.invoke("next", "web").open_uri == "https://example.org/x"
    assert host.invoke("next", "evil").open_uri is None
    # Another plugin's cache directory is off limits.
    other = cache / "plugin" / "x.pdf"
    other.write_text("x")
    assert host.client.checked_open_uri(other.as_uri()) is None

    payload = tmp_path / "photo.jpg"
    payload.write_bytes(b"x")
    assert [t.label for t in host.share_targets()] == ["iPhone (LocalSend)"]
    result = host.send("phone", [str(payload)])
    assert result.ok and result.job == "job-1"
    assert service.sent == [("phone", [str(payload.resolve())])]

    emit_card_changed(service)
    emit_notify(service, "Reminder", "Dentist at 10:00", "view-calendar", "Open", "open")
    emit_notify(service, "", "")  # nothing to show: dropped
    assert host.card_changes == 1
    assert len(host.notifications) == 1
    clicked = host.click(host.notifications[0])
    assert clicked.ok and service.clicked[-1] == ("notify", "open", {})
    # Everything is on Plugin1; the popup button passes item "notify".
    invokes = [call for call in host.transport.calls if call[1] == "InvokeAction"]
    assert {call[0] for call in invokes} == {"io.weirdware.BlueFerry.Plugin1"}
    assert invokes[-1][2][:2] == ("notify", "open")


def test_service_refuses_bad_ids_arguments_and_file_lists(cache) -> None:
    service = inline_service(_Everything, _manifest(), cache=cache)
    host = FakeHost(service, cache_root=cache)
    with pytest.raises(PluginError, match="not an action id"):
        host.client.invoke_action("../x", "open")
    with pytest.raises(PluginError, match="no such file"):
        host.send("phone", ["/nonexistent/file"])
    outcome: dict = {}
    service.InvokeAction("a", "b", "[1]", reply=outcome.setdefault,
                         error=lambda e: outcome.setdefault("error", e), sender=":1.2")
    assert isinstance(outcome["error"], PluginCallError)


def _client(replies, cache) -> PluginClient:
    return PluginClient(_manifest(), transport=ScriptedTransport(replies), cache_root=lambda: cache)


@pytest.mark.parametrize("method,reply,message", [
    ("GetCardItems", "{nope", "invalid JSON"),
    ("GetCardItems", '{"items": 3}', "invalid card"),
    ("ShareTargets", "[]", "invalid target list"),
    ("InvokeAction", '{"ok": "yes"}', "invalid answer"),
    ("SendFiles", "7", "invalid answer"),
])
def test_bad_surface_replies_are_errors_not_crashes(cache, tmp_path, method, reply, message):
    payload = tmp_path / "a.txt"
    payload.write_text("x")
    client = _client({method: reply}, cache)
    call = {
        "GetCardItems": client.card_items,
        "ShareTargets": client.share_targets,
        "InvokeAction": lambda: client.invoke_action("a", "b"),
        "SendFiles": lambda: client.send_files("t", [str(payload)]),
    }[method]
    with pytest.raises(PluginError, match=message):
        call()


def test_card_parsing_limits_counts_lengths_and_drops_junk(cache) -> None:
    raw = {"items": [
        {"id": f"i{n}", "title": "T" * 500, "subtitle": "S" * 500, "icon": "../../evil",
         "actions": [{"id": f"a{k}", "label": "L" * 99, "kind": "nuke"} for k in range(6)]}
        for n in range(12)
    ] + [{"id": "no title"}, "junk", {"id": "i0", "title": "duplicate"}]}
    items = _client({"GetCardItems": json.dumps(raw)}, cache).card_items()
    assert len(items) == sf.MAX_CARD_ITEMS
    first = items[0]
    assert len(first.title) == sf.MAX_TITLE and len(first.subtitle or "") == sf.MAX_SUBTITLE
    assert first.icon == "" and len(first.actions) == sf.MAX_ACTIONS
    assert all(len(a.label) == sf.MAX_LABEL and a.kind == "button" for a in first.actions)


@given(st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=30),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.sampled_from(["id", "title", "items", "actions", "label", "ok",
                                       "targets", "open_uri", "kind", "icon"]),
                      children, max_size=6),
    max_leaves=25,
))
def test_surface_parsers_only_raise_surface_errors(value) -> None:
    text = json.dumps(value)
    for parse in (sf.parse_card_items, sf.parse_share_targets, sf.parse_action_result,
                  sf.parse_send_result):
        try:
            parse(text)
        except sf.SurfaceError:
            pass


@given(st.text(max_size=300), st.text(max_size=400), st.text(max_size=80),
       st.text(max_size=60), st.text(max_size=80))
def test_notifications_are_plain_bounded_and_need_both_action_parts(
    title, body, icon, label, action,
) -> None:
    note = sf.parse_notification(title, body, icon, label, action)
    if note is None:
        return
    assert len(note.title) <= sf.MAX_TITLE and len(note.body) <= sf.MAX_NOTIFY_BODY
    assert all(ch.isprintable() for ch in note.title + note.body)
    assert bool(note.action_label) == bool(note.action_id)


def test_open_uri_allows_only_web_and_own_cache_files(cache, tmp_path) -> None:
    inside = cache / "plugin" / "a.pdf"
    inside.write_text("x")
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = cache / "plugin" / "link"
    link.symlink_to(outside)
    roots = [cache]
    assert sf.checked_open_uri(inside.as_uri(), roots) == inside.resolve().as_uri()
    assert sf.checked_open_uri((cache / "plugin").as_uri(), roots) is not None
    assert sf.checked_open_uri("https://example.org/a?b=c", roots) == "https://example.org/a?b=c"
    for bad in (outside.as_uri(), link.as_uri(), "file:///etc/passwd", "javascript:alert(1)",
                "https://user:pw@example.org/", "https://exa mple.org", "ftp://x/y",
                "file://otherhost" + str(inside), "https://example.org/\n", "x" * 3000, 5):
        assert sf.checked_open_uri(bad, roots) is None, bad
    assert sf.checked_open_uri(inside.as_uri(), roots, uid=os.getuid() + 1) is None


@pytest.mark.private_dbus
def test_a_combined_service_answers_on_plugin1_on_the_private_bus(cache) -> None:
    import dbus
    import dbus.mainloop
    import dbus.service
    from gi.repository import GLib

    from blueferry.plugin_api.client import DBusTransport
    from tests.private_bus import open_private_bus

    plugin = manifest(f"io.example.s{os.getpid()}", capabilities="card;notify;",
                      api_version="1.2")
    bus = dbus.SessionBus()
    name = dbus.service.BusName(plugin.bus_name, bus=bus, do_not_queue=True)
    service = _Everything(plugin, bus, cache=cache, start_worker=lambda work: work())
    outcome: dict = {}

    def request() -> None:
        connection = open_private_bus(mainloop=dbus.mainloop.NULL_MAIN_LOOP)
        try:
            client = PluginClient(plugin, transport=DBusTransport(connection),
                                  cache_root=lambda: cache)
            outcome["items"] = client.card_items()
            outcome["card"] = client.invoke_action("next", "snooze")
            outcome["notify"] = client.invoke_action("notify", "snooze", notify=True)
        except Exception as error:
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=request)
    thread.start()
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.001)
    thread.join(1)
    try:
        assert "error" not in outcome, outcome
        assert [item.id for item in outcome["items"]] == ["next", "raw"]
        assert outcome["card"].message == "snooze done"
        assert service.clicked[-2:] == [("next", "snooze", {}), ("notify", "snooze", {})]
    finally:
        service.remove_from_connection()
        del name
        bus.release_name(plugin.bus_name)


def test_files_and_clicks_never_reach_a_plugin_run_by_another_user(cache, tmp_path) -> None:
    payload = tmp_path / "a.txt"
    payload.write_text("x")
    transport = ScriptedTransport({"SendFiles": '{"ok": true}', "InvokeAction": '{"ok": true}'},
                                  uid=os.getuid() + 1)
    client = PluginClient(_manifest(), transport=transport, cache_root=lambda: cache)
    with pytest.raises(PluginError, match="different user"):
        client.send_files("t", [str(payload)])
    with pytest.raises(PluginError, match="different user"):
        client.invoke_action("a", "b")
    assert transport.calls == []
