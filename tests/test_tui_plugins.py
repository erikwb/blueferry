"""Plugin cards and "Send to…" in the Textual client (fakes only)."""
from __future__ import annotations

from textual.app import App
from textual.widgets import Button, Input, OptionList, Static

from blueferry import plugin_surfaces as surfaces
from blueferry.plugin_api.surfaces import Action, CardItem
from blueferry.plugin_surfaces_model import PluginSurfacesModel
from blueferry.tui_phone import PhoneOverviewScreen
from blueferry.tui_plugins import (
    PluginCardsPanel,
    SendToScreen,
    card_options,
    complete_path,
    split_paths,
)
from blueferry.tui_tasks import TextualTaskRunner, timer_factory
from tests.test_tui import _run_headless

CARDS = [
    surfaces.PluginCard("io.example.ls", "LocalSend", True, "", [
        CardItem("phone", "[b]iPhone[/b]\x1b[31m", subtitle="ready", actions=[
            Action("send", "Send files…", kind="primary", send_to="phone"),
            Action("ping", "[@click=app.quit]Ping[/]")]),
    ], can_send=True),
    surfaces.PluginCard("io.example.broken", "Broken", False, "Unavailable: timeout"),
]


class _Fakes:
    def __init__(self, cards=CARDS) -> None:
        self.cards = cards
        self.invoked: list[tuple] = []
        self.sent: list[tuple] = []
        self.opened: list[str] = []
        self.watched: list[list[str]] = []
        self.loads = 0
        self.choice = surfaces.ShareChoice("io.example.ls", "LocalSend", "phone", "iPhone")

    def _load(self):
        self.loads += 1
        return self.cards

    def factory(self, node):
        def make(on_change, opener, watch) -> PluginSurfacesModel:
            def watching(names):
                self.watched.append(names)
                watch(names)

            return PluginSurfacesModel(
                runner=TextualTaskRunner(node, group="test-surfaces"),
                timer=timer_factory(node), opener=opener, on_change=on_change,
                watch=watching,
                load_cards=self._load,
                load_targets=lambda: surfaces.ShareTargets([self.choice], ["Other: timeout"]),
                card_names=lambda: ["io.weirdware.BlueFerry.Plugin.ls"],
                share_available=lambda: True,
                find=lambda plugin_id, capability: plugin_id,
                invoke=lambda plugin, item, action: self.invoked.append((plugin, item, action))
                or surfaces.Outcome(True, "Pinged", "https://example.org/x"),
                send=lambda choice, files: self.sent.append((choice.key, files))
                or surfaces.Outcome(True, "Sending 1 file"),
                card_choice=lambda plugin_id, target, label: self.choice,
            )

        return make


class _Bus:
    def __init__(self) -> None:
        self.receivers: dict[str, object] = {}
        self.removed: list[str] = []

    def add_signal_receiver(self, handler, **match):
        bus = self
        name = match["bus_name"]
        assert match["signal_name"] == "CardChanged"
        self.receivers[name] = handler

        class _Match:
            def remove(self) -> None:
                bus.removed.append(name)

        return _Match()


class _PanelApp(App[None]):
    CSS_PATH = "../src/blueferry/tui.tcss"

    def __init__(self, fakes: _Fakes, bus: _Bus) -> None:
        super().__init__()
        self.fakes = fakes
        self.bus = bus

    def compose(self):
        yield PluginCardsPanel(
            lambda *args: self.fakes.factory(self.query_one(PluginCardsPanel))(*args),
            watcher_bus=lambda: self.bus, opener=self.fakes.opened.append,
        )


async def _until(pilot, condition) -> None:
    for _attempt in range(60):
        if condition():
            return
        await pilot.pause(0.05)
    assert condition()


def test_card_options_are_plain_text_with_selectable_actions() -> None:
    options, actions = card_options(surfaces.card_rows(CARDS))
    lines = [option.prompt.plain for option in options]
    assert lines[0] == "LocalSend"
    assert "[b]iPhone[/b]" in lines[1] and "\x1b" not in lines[1]
    assert lines[2].endswith("▸ Send files…")
    assert lines[3].endswith("· [@click=app.quit]Ping[/]")
    assert lines[4:] == ["Broken", "  Unavailable: timeout"]
    assert [option.disabled for option in options] == [True, True, False, False, True, True]
    assert [(plugin, action["id"]) for plugin, _item, action in actions] == [
        ("io.example.ls", "send"), ("io.example.ls", "ping")]


def test_panel_runs_actions_watches_card_plugins_and_opens_send_to() -> None:
    async def scenario() -> None:
        fakes, bus = _Fakes(), _Bus()
        app = _PanelApp(fakes, bus)
        async with app.run_test(size=(100, 40)) as pilot:
            panel = app.query_one(PluginCardsPanel)
            await _until(pilot, lambda: panel.display and panel.card_actions)
            assert sorted(bus.receivers) == ["io.weirdware.BlueFerry.Plugin.ls"]
            listing = app.query_one("#plugin-cards-list", OptionList)
            assert app.query_one("#plugin-cards-send", Button).display

            listing.focus()
            listing.highlighted = 3  # Ping
            await pilot.press("enter")
            await _until(pilot, lambda: fakes.invoked)
            assert fakes.invoked == [("io.example.ls", "phone", "ping")]
            await _until(pilot, lambda: fakes.opened)
            assert fakes.opened == ["https://example.org/x"]
            message = app.query_one("#plugin-cards-message", Static)
            assert message.render().plain == "Pinged"

            loads = fakes.loads
            bus.receivers["io.weirdware.BlueFerry.Plugin.ls"]()  # CardChanged
            await _until(pilot, lambda: fakes.loads > loads)

            listing.focus()
            listing.highlighted = 2  # Send files… (sendTo)
            await pilot.press("enter")
            await _until(pilot, lambda: isinstance(app.screen, SendToScreen))
            screen = app.screen
            assert isinstance(screen, SendToScreen)
            assert screen.card_target == ("io.example.ls", "phone", "[b]iPhone[/b]\x1b[31m")
            assert not screen.query("#send-targets")  # one fixed target
            panel.stop()
            assert bus.removed == ["io.weirdware.BlueFerry.Plugin.ls"]

    _run_headless(scenario())


def test_send_to_picks_a_target_and_closes_after_sending(tmp_path) -> None:
    payload = tmp_path / "a file.jpg"
    payload.write_bytes(b"x")

    async def scenario() -> None:
        fakes, bus = _Fakes(), _Bus()
        app = _PanelApp(fakes, bus)
        async with app.run_test(size=(100, 40)) as pilot:
            panel = app.query_one(PluginCardsPanel)
            await _until(pilot, lambda: panel.display)
            await pilot.click("#plugin-cards-send")
            await _until(pilot, lambda: isinstance(app.screen, SendToScreen))
            screen = app.screen
            targets = screen.query_one("#send-targets", OptionList)
            await _until(pilot, lambda: targets.option_count == 2)
            assert targets.get_option_at_index(0).prompt.plain == "iPhone"
            assert targets.get_option_at_index(1).disabled

            field = screen.query_one("#send-paths", Input)
            field.value = '"unclosed'
            await pilot.press("enter")
            status = screen.query_one("#send-status", Static)
            assert status.render().plain == "A quote is not closed."
            assert not fakes.sent

            field.value = str(payload).replace(" ", "\\ ")
            await pilot.press("enter")
            await _until(pilot, lambda: not isinstance(app.screen, SendToScreen))
            assert fakes.sent == [("io.example.ls:phone", [str(payload.resolve())])]

    _run_headless(scenario())


def test_send_to_catches_up_on_a_send_that_finished_under_another_screen(tmp_path) -> None:
    from textual.screen import ModalScreen

    payload = tmp_path / "b.jpg"
    payload.write_bytes(b"x")

    async def scenario() -> None:
        fakes, bus = _Fakes(), _Bus()
        app = _PanelApp(fakes, bus)
        async with app.run_test(size=(100, 40)) as pilot:
            panel = app.query_one(PluginCardsPanel)
            await _until(pilot, lambda: panel.display)
            await pilot.click("#plugin-cards-send")
            await _until(pilot, lambda: isinstance(app.screen, SendToScreen))
            screen = app.screen
            targets = screen.query_one("#send-targets", OptionList)
            await _until(pilot, lambda: targets.option_count == 2)
            screen.query_one("#send-paths", Input).value = str(payload)
            screen.send()
            # Like the command palette: the send finishes while covered.
            cover: ModalScreen[None] = ModalScreen()
            app.push_screen(cover)
            loads = fakes.loads
            await _until(pilot, lambda: fakes.sent and not panel.model.state()["busy"])
            # ... and so does the card reload after it: no change is left to
            # forward once the cover goes.
            await _until(pilot, lambda: fakes.loads > loads
                         and not panel.model.state()["busy"])
            await pilot.pause(0.1)
            assert app.screen is cover
            app.pop_screen()
            await _until(pilot, lambda: not isinstance(app.screen, SendToScreen))

    _run_headless(scenario())


def test_overview_shows_the_plugin_panel_only_when_asked() -> None:
    async def scenario() -> None:
        fakes, bus = _Fakes(), _Bus()

        class _Host(App[None]):
            CSS_PATH = "../src/blueferry/tui.tcss"

        app = _Host()
        async with app.run_test(size=(100, 50)) as pilot:
            from blueferry import phone_overview

            plain = PhoneOverviewScreen({}, phone_overview.now_playing(None))
            await app.push_screen(plain)
            assert not plain.query(PluginCardsPanel)
            plain.dismiss(None)
            await pilot.pause()

            def panel() -> PluginCardsPanel:
                widget = PluginCardsPanel(
                    lambda *args: fakes.factory(widget)(*args), watcher_bus=lambda: bus)
                return widget

            screen = PhoneOverviewScreen({}, phone_overview.now_playing(None),
                                         plugin_cards=panel)
            await app.push_screen(screen)
            cards = screen.query_one(PluginCardsPanel)
            await _until(pilot, lambda: cards.display and cards.card_actions)
            screen.dismiss(None)
            await _until(pilot, lambda: bus.removed)  # closing stops the watching

    _run_headless(scenario())


def test_split_and_complete_paths(tmp_path) -> None:
    assert split_paths('a "b c" d\\ e') == ["a", "b c", "d e"]
    (tmp_path / "Pictures").mkdir()
    (tmp_path / "Photo one.jpg").write_bytes(b"")
    (tmp_path / ".hidden").write_bytes(b"")
    base = f"x {tmp_path}/"
    assert complete_path(base + "Pi") == base + "Pictures/"
    assert complete_path(base + "Ph") == base + "Photo\\ one.jpg"
    assert complete_path(base + "P") is None  # ambiguous, no common rest
    assert complete_path(base + ".h") == base + ".hidden"
    assert complete_path(base + "'Pi") is None
    assert complete_path("") is None
    # An escaped space or a quote keeps the last word together: no
    # completion of its tail against the current directory.
    assert complete_path(base + "Photo\\ on") is None
    assert complete_path(f'x "{tmp_path}/Photo on') is None
    quoted = f'"{tmp_path}/a \\" b" {tmp_path}/'
    assert complete_path(quoted + "Pi") == quoted + "Pictures/"
    assert split_paths(complete_path(base + "Ph") or "")[1] == f"{tmp_path}/Photo one.jpg"
    # A quote or backslash inside the completed name is escaped too, so
    # split_paths reads the suggestion back as that one file.
    for name, prefix in (("it's.jpg", "it"), ('say "hi".jpg', "say"),
                         ("back\\slash.jpg", "back")):
        (tmp_path / name).write_bytes(b"")
        assert split_paths(complete_path(base + prefix) or "")[1] == f"{tmp_path}/{name}"
