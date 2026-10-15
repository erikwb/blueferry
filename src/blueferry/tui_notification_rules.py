"""Notification click-rule editor for the Textual client.

The panel is a modal screen opened with ``l``. It lists, adds and removes the
per-app rules that decide what clicking a mirrored iPhone notification opens
(an http(s) URL or a desktop entry ID). The backend validates every rule and
stays the authority; the panel validates locally only for a quick, specific
message. Every backend call runs in a worker thread.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Protocol

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from blueferry.client import BackendError
from blueferry.notification_open_map import parse_target, validate_bundle_id
from blueferry.text_safety import terminal_text

# Same bounds as the Qt editor's fields.
_MAX_BUNDLE = 255
_MAX_TARGET = 2048
REVISION_KEY = "notification_open_map_revision"

INTRO = (
    "Choose a web address (http or https) or a desktop app to open when a "
    "notification from an iPhone app is clicked. Apps without a rule keep "
    "the default behaviour. Notification text is never passed to the app or "
    "address."
)
POLICY_NOTE = (
    "Rules apply only while All iPhone Notifications is selected in the "
    "BlueFerry settings."
)


class NotificationRulesClient(Protocol):
    def notification_open_map(self) -> list[dict[str, str]]: ...

    def set_notification_open_target(
        self, bundle_id: str, target: str,
    ) -> list[dict[str, str]]: ...

    def remove_notification_open_target(self, bundle_id: str) -> bool: ...


def rule_line(rule: dict[str, str], width: int) -> str:
    """One list row; stored values are validated ASCII but escaped anyway."""
    bundle = terminal_text(rule.get("bundle_id", "")).replace("\n", " ")
    kind = terminal_text(rule.get("kind", "")).replace("\n", " ")
    target = terminal_text(rule.get("target", "")).replace("\n", " ")
    return f"{bundle.ljust(width)}  {kind:<7}  {target}"


def validate_rule(bundle_id: str, target: str) -> str | None:
    """Return a message for an invalid draft, or None when it may be sent."""
    if not bundle_id or not target:
        return "Enter an iPhone app bundle ID and a target"
    try:
        validate_bundle_id(bundle_id)
        parse_target(target)
    except ValueError as error:
        return str(error)
    return None


class NotificationRulesScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(
        self,
        client: NotificationRulesClient,
        *,
        policy_all: bool = True,
    ) -> None:
        super().__init__()
        self._client = client
        self._policy_all = policy_all
        self.rules: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="rules-dialog", classes="dialog"):
            yield Static("Clicking iPhone notifications", classes="dialog-title")
            yield Static(Text(INTRO), classes="dialog-copy")
            if not self._policy_all:
                yield Static(Text(POLICY_NOTE), id="rules-policy-note")
            yield Static("Loading…", id="rules-state")
            yield OptionList(id="rules-list")
            yield Static("iPhone app bundle ID", classes="field-label")
            yield Input(
                placeholder="com.apple.mobilemail",
                id="rules-bundle",
                max_length=_MAX_BUNDLE,
            )
            yield Static("Web address or desktop app ID", classes="field-label")
            yield Input(
                placeholder="https://… or org.mozilla.Thunderbird.desktop",
                id="rules-target",
                max_length=_MAX_TARGET,
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="rules-close")
                yield Button("Remove", variant="error", id="rules-remove", disabled=True)
                yield Button("Save", variant="primary", id="rules-save")

    def on_mount(self) -> None:
        self.query_one("#rules-bundle", Input).focus()
        self.reload()

    @work(thread=True, exclusive=True, group="rules-load", exit_on_error=False)
    def reload(self) -> None:
        try:
            rules = self._client.notification_open_map()
        except BackendError as error:
            self.app.call_from_thread(self._show_error, str(error))
            return
        self.app.call_from_thread(self._show, rules)

    def _show(self, rules: list[dict[str, str]]) -> None:
        self.rules = list(rules)
        options = self.query_one("#rules-list", OptionList)
        options.clear_options()
        width = max((len(rule.get("bundle_id", "")) for rule in self.rules), default=0)
        # Plain Text, never markup: a hand-edited settings file is validated
        # by the backend, but the terminal boundary escapes regardless.
        options.add_options([
            Option(Text(rule_line(rule, width)), id=rule.get("bundle_id") or None)
            for rule in self.rules
        ])
        state = (
            "No rules yet." if not self.rules
            else f"{len(self.rules)} rule{'s' if len(self.rules) != 1 else ''}"
        )
        self.query_one("#rules-state", Static).update(Text(state))
        self._update_remove()

    def _show_error(self, message: str) -> None:
        self.query_one("#rules-state", Static).update(Text(terminal_text(message)))

    def _selected_bundle(self) -> str | None:
        options = self.query_one("#rules-list", OptionList)
        index = options.highlighted
        if index is None or not 0 <= index < len(self.rules):
            return None
        return self.rules[index].get("bundle_id") or None

    def _update_remove(self) -> None:
        self.query_one("#rules-remove", Button).disabled = self._selected_bundle() is None

    @on(OptionList.OptionHighlighted, "#rules-list")
    def rule_highlighted(self) -> None:
        self._update_remove()

    @on(OptionList.OptionSelected, "#rules-list")
    def rule_selected(self) -> None:
        # Enter on a rule copies it into the form for editing.
        index = self.query_one("#rules-list", OptionList).highlighted
        if index is None or not 0 <= index < len(self.rules):
            return
        rule = self.rules[index]
        self.query_one("#rules-bundle", Input).value = rule.get("bundle_id", "")
        self.query_one("#rules-target", Input).value = rule.get("target", "")
        self.query_one("#rules-target", Input).focus()

    @work(thread=True, group="rules-action", exit_on_error=False)
    def _run(
        self,
        action: Callable[[], object],
        done: Callable[[object], None],
    ) -> None:
        try:
            result = action()
        except BackendError as error:
            message = terminal_text(str(error))
            self.app.call_from_thread(
                lambda: self.notify(message, severity="error", markup=False)
            )
            return
        self.app.call_from_thread(done, result)

    @on(Input.Submitted, "#rules-bundle")
    def bundle_submitted(self) -> None:
        self.query_one("#rules-target", Input).focus()

    @on(Button.Pressed, "#rules-save")
    @on(Input.Submitted, "#rules-target")
    def save_rule(self) -> None:
        draft = (
            self.query_one("#rules-bundle", Input).value,
            self.query_one("#rules-target", Input).value,
        )
        bundle, target = (value.strip() for value in draft)
        problem = validate_rule(bundle, target)
        if problem is not None:
            self.notify(terminal_text(problem), severity="warning", markup=False)
            return

        def saved(result: object) -> None:
            if isinstance(result, list):
                self._show(result)
            # A save runs in a worker; the user may already be editing the
            # next rule when it finishes. Clear only the submitted draft.
            bundle_input = self.query_one("#rules-bundle", Input)
            target_input = self.query_one("#rules-target", Input)
            if (bundle_input.value, target_input.value) == draft:
                bundle_input.value = ""
                target_input.value = ""
            self.notify("Click rule saved")

        self._run(
            lambda: self._client.set_notification_open_target(bundle, target),
            saved,
        )

    @on(Button.Pressed, "#rules-remove")
    def remove_rule(self) -> None:
        bundle = self._selected_bundle()
        if bundle is None:
            return

        def removed(result: object) -> None:
            self.notify("Click rule removed" if result else "No such rule")
            self.reload()

        self._run(
            lambda: self._client.remove_notification_open_target(bundle),
            removed,
        )

    @on(Button.Pressed, "#rules-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
