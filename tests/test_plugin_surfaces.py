"""Host side of the plugin surfaces: card rows, actions, Send to… and the CLI."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from blueferry import cli_surfaces
from blueferry import plugin_surfaces as surfaces
from blueferry.cli import app
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.manifest import Discovery
from blueferry.plugin_api.testing import ScriptedTransport, manifest

CALENDAR = manifest("io.example.calendar", capabilities="card;notify;", api_version="1.2")
LOCALSEND = manifest("io.example.localsend", capabilities="share;card;", api_version="1.2",
                     extra="Alias=localsend\n")
WEBDAV = manifest("io.example.webdav", capabilities="share;", api_version="1.2")
PHOTOS = manifest("io.example.photos")


def _factory(replies_by_plugin, cache=None):
    def make(plugin):
        replies = replies_by_plugin[plugin.id]
        return PluginClient(plugin, transport=ScriptedTransport(replies),
                            cache_root=(lambda: cache) if cache else None)
    return make


def test_surface_plugins_filters_by_capability_and_disabled() -> None:
    found = Discovery((CALENDAR, LOCALSEND, WEBDAV, PHOTOS))
    assert [p.id for p in surfaces.surface_plugins("card", found, frozenset())] == [
        CALENDAR.id, LOCALSEND.id]
    assert [p.id for p in surfaces.surface_plugins("share", found, frozenset({WEBDAV.id}))] == [
        LOCALSEND.id]


def test_a_crashing_or_garbled_plugin_becomes_a_dimmed_hint() -> None:
    def crash(*_args):
        raise PluginError("the plugin did not answer in time")

    cards = surfaces.load_cards([CALENDAR, LOCALSEND, WEBDAV], client_factory=_factory({
        CALENDAR.id: {"GetCardItems": json.dumps({"items": [
            {"id": "next", "icon": "view-calendar", "title": "Dentist", "subtitle": "10:00",
             "actions": [{"id": "open", "label": "Open", "kind": "primary"}]}]})},
        LOCALSEND.id: {"GetCardItems": crash},
        WEBDAV.id: {"GetCardItems": "{broken"},
    }))
    assert [(c.name, c.ok) for c in cards] == [("Example", True), ("Example", False),
                                               ("Example", False)]
    assert "did not answer" in cards[1].hint and "invalid JSON" in cards[2].hint
    rows = surfaces.card_rows(cards)
    assert rows[0]["items"][0]["actions"] == [
        {"id": "open", "label": "Open", "icon": "", "primary": True, "sendTo": ""}]
    assert rows[1]["items"] == [] and rows[1]["ok"] is False


def test_a_plugin_raising_anything_never_escapes() -> None:
    def factory(_plugin):
        raise RuntimeError("boom")

    cards = surfaces.load_cards([CALENDAR], client_factory=factory)
    assert cards[0].ok is False and "RuntimeError" in cards[0].hint
    targets = surfaces.load_targets([WEBDAV], client_factory=factory)
    assert targets.choices == [] and "RuntimeError" in targets.problems[0]


def test_invoke_checks_open_uri_and_explains_failures(tmp_path) -> None:
    cache = tmp_path / "blueferry"
    (cache / CALENDAR.id).mkdir(parents=True)
    ics = cache / CALENDAR.id / "a.ics"
    ics.write_text("x")
    make = _factory({CALENDAR.id: {"InvokeAction": lambda item, action, args: json.dumps(
        {"ok": True, "message": f"{item}/{action}",
         "open_uri": ics.as_uri() if action == "open" else "file:///etc/shadow"})}}, cache)
    opened = surfaces.invoke(CALENDAR, "next", "open", client_factory=make)
    assert opened.ok and opened.open_uri == ics.resolve().as_uri()
    assert surfaces.invoke(CALENDAR, "next", "x", client_factory=make).open_uri is None
    gone = surfaces.invoke(None, "next", "open", client_factory=make)
    assert not gone.ok and "no longer" in gone.message
    failing = _factory({CALENDAR.id: {"InvokeAction": '{"ok": false}'}})
    assert surfaces.invoke(CALENDAR, "a", "b", client_factory=failing).message == (
        "Example could not do that.")


def test_share_targets_resolve_by_id_alias_and_target() -> None:
    targets = surfaces.load_targets([LOCALSEND, WEBDAV], client_factory=_factory({
        LOCALSEND.id: {"ShareTargets": json.dumps({"targets": [
            {"id": "iphone", "label": "iPhone", "icon": "smartphone"}]})},
        WEBDAV.id: {"ShareTargets": json.dumps({"targets": [
            {"id": "ablage", "label": "Ablage", "icon": "folder-cloud"},
            {"id": "iphone", "label": "iPhone", "icon": ""}]})},
    }))
    choices = targets.choices
    assert [c.key for c in choices] == [
        "io.example.localsend:iphone", "io.example.webdav:ablage", "io.example.webdav:iphone"]
    assert surfaces.choice_label(choices[0], choices) == "iPhone (Example)"
    assert surfaces.resolve_choice(choices, "localsend") == choices[0]
    assert surfaces.resolve_choice(choices, "io.example.webdav:ablage") == choices[1]
    with pytest.raises(LookupError, match="several targets"):
        surfaces.resolve_choice(choices, "io.example.webdav")
    with pytest.raises(LookupError, match="--to"):
        surfaces.resolve_choice(choices, None)
    with pytest.raises(LookupError, match="Unknown target"):
        surfaces.resolve_choice(choices, "nope")
    with pytest.raises(LookupError, match="No plugin offers"):
        surfaces.resolve_choice([], None)


def test_send_reports_the_plugin_answer(tmp_path) -> None:
    payload = tmp_path / "a.jpg"
    payload.write_bytes(b"x")
    choice = surfaces.ShareChoice(WEBDAV.id, "WebDAV", "ablage", "Ablage")
    seen = []

    def accept(target, paths):
        seen.append((target, list(paths)))
        return json.dumps({"ok": True, "message": None, "job": "j1"})

    sent = surfaces.send(choice, [str(payload)], plugin=WEBDAV,
                         client_factory=_factory({WEBDAV.id: {"SendFiles": accept}}))
    assert sent.ok and sent.message == "Sent to Ablage." and sent.job == "j1"
    assert seen == [("ablage", [str(payload.resolve())])]
    refused = surfaces.send(choice, ["/no/such/file"], plugin=WEBDAV,
                            client_factory=_factory({WEBDAV.id: {"SendFiles": accept}}))
    assert not refused.ok and "no such file" in refused.message


def test_cli_send_lists_resolves_and_reports(monkeypatch, tmp_path) -> None:
    payload = tmp_path / "a.jpg"
    payload.write_bytes(b"x")
    choices = [surfaces.ShareChoice(LOCALSEND.id, "LocalSend", "iphone", "iPhone", "",
                                    "localsend"),
               surfaces.ShareChoice(WEBDAV.id, "WebDAV", "ablage", "Ablage")]
    sent = []
    monkeypatch.setitem(cli_surfaces._hooks, "targets",
                        lambda: surfaces.ShareTargets(choices, ["Broken: timeout"]))
    monkeypatch.setitem(cli_surfaces._hooks, "send", lambda choice, paths: (
        sent.append((choice.key, paths)) or surfaces.Outcome(True, "Sending 1 file")))
    runner = CliRunner()
    listed = runner.invoke(app, ["send", "--list"])
    assert "io.example.localsend:iphone  iPhone" in listed.output
    ambiguous = runner.invoke(app, ["send", str(payload)])
    assert ambiguous.exit_code == 2 and "--to" in ambiguous.output
    done = runner.invoke(app, ["send", str(payload), "--to", "localsend"])
    assert done.exit_code == 0 and "Sending 1 file" in done.output
    assert sent == [("io.example.localsend:iphone", [str(payload.resolve())])]
    missing = runner.invoke(app, ["send", str(tmp_path / "nope"), "--to", "localsend"])
    assert missing.exit_code == 2 and "no such file" in missing.output


def test_cli_cards_prints_items_and_runs_actions(monkeypatch) -> None:
    from blueferry.plugin_api.surfaces import Action, CardItem

    monkeypatch.setitem(cli_surfaces._hooks, "cards", lambda *_plugins: [
        surfaces.PluginCard(CALENDAR.id, "Calendar", True, "", [
            CardItem("next", "Dentist", subtitle="10:00", actions=[Action("open", "Open")])]),
        surfaces.PluginCard(LOCALSEND.id, "LocalSend", False, "Unavailable: timeout"),
    ])
    opened = []
    monkeypatch.setitem(cli_surfaces._hooks, "find", lambda plugin_id, cap: CALENDAR)
    monkeypatch.setitem(cli_surfaces._hooks, "invoke", lambda plugin, item, action: (
        surfaces.Outcome(True, f"{item}:{action}", "https://example.org/")))
    monkeypatch.setitem(cli_surfaces._hooks, "open_uri", opened.append)
    runner = CliRunner()
    shown = runner.invoke(app, ["cards"])
    assert "Dentist — 10:00" in shown.output
    assert "[io.example.calendar:next:open] Open" in shown.output
    assert "Unavailable: timeout" in shown.output
    ran = runner.invoke(app, ["cards", "--run", "io.example.calendar:next:open"])
    assert ran.exit_code == 0 and "next:open" in ran.output
    assert opened == ["https://example.org/"]
    assert runner.invoke(app, ["cards", "--run", "bad"]).exit_code == 2


def test_more_card_plugins_than_shown_are_named_in_one_line() -> None:
    many = [manifest(f"io.example.p{n}", capabilities="card;", api_version="1.2")
            for n in range(surfaces.MAX_CARD_PLUGINS + 2)]
    cards = surfaces.load_cards(many, client_factory=_factory({
        plugin.id: {"GetCardItems": '{"items": []}'} for plugin in many}))
    assert len(cards) == surfaces.MAX_CARD_PLUGINS + 1
    assert not cards[-1].ok and cards[-1].hint.startswith("Not shown: Example, Example.")


def test_card_choice_needs_an_enabled_share_plugin(monkeypatch) -> None:
    from blueferry.plugin_api.testing import manifest

    sharing = manifest("io.example.ls", capabilities="card;share;", api_version="1.4",
                       extra="Alias=ls\n")
    monkeypatch.setattr(surfaces, "find_plugin", lambda plugin_id, capability: (
        sharing if capability == "share" and plugin_id == "io.example.ls" else None))
    factory = _factory({sharing.id: {"ShareTargets": json.dumps({"targets": [
        {"id": "ls-1", "label": "iPhone", "icon": "phone"}]})}})
    choice = surfaces.card_choice("io.example.ls", "ls-1", "iPhone\n",
                                  client_factory=factory)
    assert choice is not None and choice.key == "io.example.ls:ls-1"
    assert choice.label == "iPhone" and choice.plugin_alias == "ls"
    assert surfaces.card_choice("io.example.other", "ls-1") is None
    assert surfaces.card_choice("io.example.ls", "bad:id") is None
    # A card older than the device list: the target is gone.
    with pytest.raises(LookupError, match="no longer there"):
        surfaces.card_choice("io.example.ls", "ls-2", "iPad", client_factory=factory)


def test_cli_cards_runs_a_sending_action_with_files(monkeypatch, tmp_path) -> None:
    from blueferry.plugin_api.surfaces import Action, CardItem

    payload = tmp_path / "a.jpg"
    payload.write_bytes(b"x")
    card = surfaces.PluginCard(LOCALSEND.id, "LocalSend", True, "", [
        CardItem("dev-phone", "iPhone", actions=[
            Action("send", "Send files…", kind="primary", send_to="ls-1"),
            Action("trust", "Always accept")])], can_send=True)
    asked: list[list] = []
    monkeypatch.setitem(cli_surfaces._hooks, "cards",
                        lambda *plugins: asked.append(list(plugins)) or [card])
    monkeypatch.setitem(cli_surfaces._hooks, "find", lambda plugin_id, cap: LOCALSEND)
    invoked, sent = [], []
    monkeypatch.setitem(cli_surfaces._hooks, "invoke", lambda plugin, item, action: (
        invoked.append((item, action)) or surfaces.Outcome(True, "Trusted")))
    monkeypatch.setitem(cli_surfaces._hooks, "card_choice", lambda plugin, target, title: (
        surfaces.ShareChoice(plugin, "LocalSend", target, title)))
    monkeypatch.setitem(cli_surfaces._hooks, "send", lambda choice, paths: (
        sent.append((choice.key, choice.label, paths)) or surfaces.Outcome(True, "Sending")))
    runner = CliRunner()
    listed = runner.invoke(app, ["cards"])
    assert "Send files… (sends files: add FILE…)" in listed.output
    action = f"{LOCALSEND.id}:dev-phone:send"
    without = runner.invoke(app, ["cards", "--run", action])
    assert without.exit_code == 2 and "sends files" in without.output
    done = runner.invoke(app, ["cards", "--run", action, str(payload)])
    assert done.exit_code == 0 and "Sending" in done.output
    assert sent == [(f"{LOCALSEND.id}:ls-1", "iPhone", [str(payload.resolve())])]
    extra = runner.invoke(app, ["cards", "--run", f"{LOCALSEND.id}:dev-phone:trust", "x"])
    assert extra.exit_code == 2 and "takes no files" in extra.output
    trusted = runner.invoke(app, ["cards", "--run", f"{LOCALSEND.id}:dev-phone:trust"])
    assert trusted.exit_code == 0 and invoked == [("dev-phone", "trust")]
    assert [[LOCALSEND]] in asked  # only that plugin is asked for its card
