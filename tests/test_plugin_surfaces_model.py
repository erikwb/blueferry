""""From Plugins" and "Send to…" logic shared by the clients: no toolkit, fakes only."""
from __future__ import annotations

from blueferry import plugin_surfaces as surfaces
from blueferry.plugin_api.surfaces import Action, CardItem
from blueferry.plugin_surfaces_model import RELOAD_DELAY_MS, PluginSurfacesModel
from tests.client_fakes import ManualRunner

CARDS = [
    surfaces.PluginCard("io.example.calendar", "Calendar", True, "", [
        CardItem("next", "Dentist", icon="view-calendar", subtitle="10:00", actions=[
            Action("open", "Open", kind="primary"), Action("snooze", "Snooze")])]),
    surfaces.PluginCard("io.example.broken", "Broken", False, "Unavailable: timeout"),
]


def _model(work: ManualRunner, opened: list[str] | None = None, **kwargs) -> PluginSurfacesModel:
    kwargs.setdefault("load_cards", lambda: [])
    kwargs.setdefault("share_available", lambda: False)
    kwargs.setdefault("card_names", lambda: [])
    return PluginSurfacesModel(
        runner=work, timer=work.timer,
        opener=(opened if opened is not None else []).append, **kwargs,
    )


def test_reload_invoke_and_send_run_on_the_runner(tmp_path) -> None:
    work = ManualRunner()
    opened: list[str] = []
    invoked: list[tuple] = []
    sent: list[tuple] = []
    changes: list[None] = []
    choice = surfaces.ShareChoice("io.example.ls", "LocalSend", "iphone", "iPhone", "smartphone")
    model = _model(
        work, opened,
        on_change=lambda: changes.append(None),
        load_cards=lambda: CARDS,
        load_targets=lambda: surfaces.ShareTargets([choice], ["Other: timeout"]),
        share_available=lambda: True,
        find=lambda plugin_id, capability: plugin_id,
        invoke=lambda plugin, item, action: invoked.append((plugin, item, action))
        or surfaces.Outcome(True, "Opened", "https://example.org/e"),
        send=lambda target, files: sent.append((target.key, files))
        or surfaces.Outcome(True, "Sending 1 file"),
    )
    model.reload()
    assert model.state()["loaded"] is False and work.pending  # off the UI thread
    work.run_tasks()
    assert changes
    cards = model.state()["cards"]
    assert model.state()["shareAvailable"] is True
    assert model.state()["targetsLoaded"] is False  # targets start plugins: not yet
    assert cards[0]["items"][0]["actions"][0] == {
        "id": "open", "label": "Open", "icon": "", "primary": True, "sendTo": ""}
    assert cards[1]["ok"] is False and cards[1]["hint"] == "Unavailable: timeout"

    model.invoke("io.example.calendar", "next", "open")
    assert model.state()["busy"] == "io.example.calendar:next:open"
    model.invoke("io.example.calendar", "next", "snooze")  # busy: ignored
    work.run_tasks()
    assert model.state()["message"] == "Opened" and model.state()["busy"] == ""
    assert invoked == [("io.example.calendar", "next", "open")]
    assert opened == ["https://example.org/e"]
    assert model._timer.delay == RELOAD_DELAY_MS  # the card reloads afterwards

    model.load_targets()
    work.run_tasks()
    assert model.state()["targets"] == [
        {"key": "io.example.ls:iphone", "icon": "smartphone", "label": "iPhone",
         "menuText": "iPhone"}]
    assert model.state()["targetProblems"] == ["Other: timeout"]
    payload = tmp_path / "a.jpg"
    payload.write_bytes(b"x")
    model.send("io.example.ls:iphone", [str(payload)])
    assert model.state()["busy"] == "send:io.example.ls:iphone"
    work.run_tasks()
    assert model.state()["message"] == "Sending 1 file"
    assert sent == [("io.example.ls:iphone", [str(payload.resolve())])]
    model.send("io.example.ls:iphone", ["/no/such/file"])
    work.run_tasks()
    assert "no such file" in model.state()["message"]
    model.send("gone:target", [str(payload)])
    assert model.state()["messageOk"] is False and not work.pending


def test_an_action_of_a_plugin_disabled_meanwhile_opens_nothing() -> None:
    work = ManualRunner()
    opened: list[str] = []
    installed = {"io.example.calendar"}

    def invoke(plugin, item, action):
        installed.clear()  # disabled while the plugin answered
        return surfaces.Outcome(True, "Opened", "https://example.org/e")

    model = _model(work, opened, load_cards=lambda: CARDS,
                   find=lambda plugin_id, capability: plugin_id if plugin_id in installed else None,
                   invoke=invoke)
    model.invoke("io.example.calendar", "next", "open")
    work.run_tasks()
    assert model.state()["message"] == "Opened" and opened == []


def test_a_second_send_waits_for_the_first(tmp_path) -> None:
    work = ManualRunner()
    payload = tmp_path / "a.jpg"
    payload.write_bytes(b"x")
    sent: list[str] = []
    choice = surfaces.ShareChoice("io.example.ls", "LocalSend", "iphone", "iPhone")
    model = _model(work, share_available=lambda: True,
                   load_targets=lambda: surfaces.ShareTargets([choice], []),
                   send=lambda target, files: sent.append(target.key)
                   or surfaces.Outcome(True, "Sending 1 file"))
    model.load_targets()
    work.run_tasks()
    model.send("io.example.ls:iphone", [str(payload)])
    model.send("io.example.ls:iphone", [str(payload)])  # still busy: ignored
    model.send_from_card("io.example.ls", "iphone", "iPhone", [str(payload)])  # also busy
    work.run_tasks()
    assert model.state()["message"] == "Sending 1 file"
    assert sent == ["io.example.ls:iphone"]


def test_a_crashing_loader_never_breaks_the_card() -> None:
    def boom():
        raise RuntimeError("plugin exploded")

    work = ManualRunner()
    model = _model(work, load_cards=boom)
    model.reload()
    work.run_tasks()
    assert model.state()["loaded"] is True
    assert model.state()["cards"] == [] and model.state()["messageOk"] is False
    assert model.state()["message"] == "plugin exploded"
    model.reload()  # not stuck "loading"
    assert work.pending


def test_card_changes_coalesce_into_one_reload_and_name_the_watched_plugins() -> None:
    work = ManualRunner()
    watched: list[list[str]] = []
    loads: list[None] = []
    names = ["io.weirdware.BlueFerry.Plugin.io.example.a", ""]
    model = _model(work, watch=watched.append, card_names=lambda: names,
                   load_cards=lambda: loads.append(None) or [])
    model.schedule_reload()
    model.schedule_reload()  # restarts the same timer
    assert model._timer.starts == 2 and model._timer.delay == RELOAD_DELAY_MS
    model._timer.fire()
    model.reload()  # a reload while one runs only marks another one
    assert len(work.pending) == 1
    work.run_tasks()
    assert watched == [["io.weirdware.BlueFerry.Plugin.io.example.a"]]  # no empty names
    assert model._timer.active  # the marked reload follows, coalesced
    work.drain()
    assert len(loads) == 2


def test_send_from_card_resolves_the_target_on_the_runner(tmp_path) -> None:
    picked = tmp_path / "a.txt"
    picked.write_text("x")
    sent: list[tuple] = []
    choice = surfaces.ShareChoice("io.example.ls", "LocalSend", "ls-1", "iPhone")
    asked: list[tuple] = []
    work = ManualRunner()
    model = _model(work, share_available=lambda: True,
                   card_choice=lambda p, t, label: asked.append((p, t, label)) or choice,
                   send=lambda target, files: sent.append((target.key, files))
                   or surfaces.Outcome(True, "Sending 1 file to iPhone"))
    model.send_from_card("io.example.ls", "ls-1", "iPhone", [])  # nothing picked
    assert model.state()["busy"] == "" and not work.pending
    model.send_from_card("io.example.ls", "ls-1", "iPhone", [str(picked)])
    assert asked == []  # resolved on the worker, not here
    work.run_tasks()
    assert model.state()["message"] == "Sending 1 file to iPhone"
    assert asked == [("io.example.ls", "ls-1", "iPhone")]
    assert sent == [("io.example.ls:ls-1", [str(picked)])]

    gone = _model(work, share_available=lambda: True, card_choice=lambda *_args: None)
    gone.send_from_card("io.example.ls", "ls-1", "iPhone", [str(picked)])
    work.run_tasks()
    assert gone.state()["messageOk"] is False
    assert gone.state()["message"] == "The plugin can no longer send files."


def test_a_stale_card_target_says_to_refresh(tmp_path) -> None:
    picked = tmp_path / "a.txt"
    picked.write_text("x")

    def stale(*_args):
        raise LookupError("iPhone is no longer there. Refresh the card and try again.")

    work = ManualRunner()
    model = _model(work, share_available=lambda: True, card_choice=stale)
    model.send_from_card("io.example.ls", "ls-1", "iPhone", [str(picked)])
    work.run_tasks()
    assert model.state()["messageOk"] is False and "Refresh" in model.state()["message"]


SEND_CARDS = [
    surfaces.PluginCard("io.example.ls", "LocalSend", True, "", [
        CardItem("dev-phone", "iPhone", icon="phone", subtitle="iPhone · verified", actions=[
            Action("send", "Send files…", "document-send", "primary", send_to="ls-1"),
            Action("trust", "Always accept")]),
    ], can_send=True),
]


def test_card_rows_carry_send_targets_only_for_share_plugins() -> None:
    rows = surfaces.card_rows(SEND_CARDS)
    item = rows[0]["items"][0]
    assert item["dropTarget"] == "ls-1"
    assert [a["sendTo"] for a in item["actions"]] == ["ls-1", ""]
    without_share = [surfaces.PluginCard("io.example.ls", "LocalSend", True, "",
                                         SEND_CARDS[0].items)]
    item = surfaces.card_rows(without_share)[0]["items"][0]
    assert item["dropTarget"] == "" and item["actions"][0]["sendTo"] == ""


def test_button_labels_from_plugins_are_escaped_and_titles_stay_plain() -> None:
    card = surfaces.PluginCard("io.example.x", "X", True, "", [
        CardItem("one", "<b>Title</b>", subtitle="a & b", actions=[
            Action("open", "<a href='https://evil.example'>Open</a>", kind="primary"),
            Action("keep", "Tom & Jerry")])])
    item = surfaces.card_rows([card])[0]["items"][0]
    assert item["title"] == "<b>Title</b>" and item["subtitle"] == "a & b"  # plain labels
    assert item["actions"][0]["label"] == (
        "&lt;a href='https://evil.example'&gt;Open&lt;/a&gt;")
    assert item["actions"][1]["label"] == "Tom & Jerry"  # no "<": plain already
