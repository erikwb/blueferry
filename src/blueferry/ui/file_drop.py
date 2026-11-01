"""The GTK main window's drop zone over :mod:`blueferry.file_drop_view`.

Files dragged anywhere onto the window show "Drop to send to iPhone"; on
release their URIs (``Gio.File.get_uri``, so only ``file://`` counts) go to
the shared :class:`~blueferry.file_drop_view.FileDrop`, which picks the
"Send to…" target. A card item that takes files has its own
``Gtk.DropTarget`` deeper in the widget tree, so GTK hands drops on it to
that item. Several targets open an ``AdwAlertDialog`` with one radio button
each; none opens an explanation with a way to Settings > Plugins. Notices
are toasts; a failure stays longer, like the Qt client's "long" notice.
A drop waits while the plugin card's "Send to…" is sending
(``is_sending``). Every text comes from the presenter as plain text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gdk, Gtk

from blueferry import file_drop_view as view
from blueferry.file_drop_view import FileDrop
from blueferry.i18n import _

# ``factory(notify=..., on_change=..., on_sent=..., is_sending=...)`` -> FileDrop
FileDropFactory = Callable[..., FileDrop]


def default_file_drop(client) -> FileDropFactory:
    def make(**callbacks: Any) -> FileDrop:
        return FileDrop(runner=client.task_runner("file-drop"), **callbacks)

    return make


def dropped_uris(value: object) -> list[str]:
    """The URIs of a ``Gdk.FileList``; anything else drops nothing."""
    if not isinstance(value, Gdk.FileList):
        return []
    return [str(file.get_uri() or "") for file in value.get_files()]


class FileDropZone:
    def __init__(
        self,
        window: Gtk.Widget,
        factory: FileDropFactory,
        *,
        toast: Callable[[str, bool], None],
        open_plugin_settings: Callable[[], None],
        on_sent: Callable[[], None] = lambda: None,
        is_sending: Callable[[], bool] = lambda: False,
    ) -> None:
        self._window = window
        self._toast = toast
        self._open_plugin_settings = open_plugin_settings
        self.dialog: Adw.AlertDialog | None = None
        self._dialog_phase = ""
        self._selected = ""
        self.choice_buttons: dict[str, Gtk.CheckButton] = {}
        self.model = factory(notify=self._notify, on_change=self._render, on_sent=on_sent,
                             is_sending=is_sending)

        status = Adw.StatusPage(
            icon_name="document-send-symbolic", title=view.overlay_title(),
            description=view.overlay_subtitle(),
        )
        # The hint is only drawn: the drop goes to the overlay's controller.
        self.hint = Gtk.Box(
            css_classes=["card"], can_target=False, visible=False,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
        )
        status.set_hexpand(True)
        self.hint.append(status)

        self.target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self.target.connect("enter", self._enter)
        self.target.connect("leave", self._leave)
        self.target.connect("drop", self._drop)

    def attach(self, overlay: Gtk.Overlay) -> None:
        overlay.add_overlay(self.hint)
        overlay.add_controller(self.target)

    # ---- drag and drop ------------------------------------------------------------

    def _enter(self, _target, _x, _y) -> Gdk.DragAction:
        self.hint.set_visible(True)
        return Gdk.DragAction.COPY

    def _leave(self, _target) -> None:
        self.hint.set_visible(False)

    def _drop(self, _target, value, _x, _y) -> bool:
        self.hint.set_visible(False)
        uris = dropped_uris(value)
        if not uris:
            return False
        self.drop(uris)
        return True

    def drop(self, uris: list[str]) -> None:
        self.model.drop(uris)

    # ---- feedback -----------------------------------------------------------------

    def _notify(self, text: str, ok: bool) -> None:
        if text:
            self._toast(text, not ok)  # a failure stays longer

    def _render(self) -> None:
        state = self.model.state()
        phase = state["phase"]
        if phase == self._dialog_phase:
            return
        self._close_dialog()
        if phase == view.PICKING:
            self._show_picker(state)
        elif phase == view.NO_TARGET:
            self._show_no_target(state)

    def _close_dialog(self) -> None:
        dialog, self.dialog, self._dialog_phase = self.dialog, None, ""
        if dialog is not None:
            dialog.force_close()

    def _present(self, dialog: Adw.AlertDialog, phase: str, responded) -> None:
        self.dialog, self._dialog_phase = dialog, phase

        def on_response(source: Adw.AlertDialog, response: str) -> None:
            if self.dialog is not source:
                return  # closed because the drop moved on
            self.dialog, self._dialog_phase = None, ""
            responded(response)

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _show_picker(self, state: dict) -> None:
        dialog = Adw.AlertDialog(heading=state["title"], body=state["body"])
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("send", view.send_label())
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")
        self._selected = str(state["selected"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        group: Gtk.CheckButton | None = None
        self.choice_buttons = {}
        for row in state["choices"]:
            key = str(row["key"])
            button = Gtk.CheckButton(label=str(row["label"]), use_underline=False)
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.set_active(key == self._selected)
            button.connect("toggled", self._choice_toggled, key)
            self.choice_buttons[key] = button
            box.append(button)
        dialog.set_extra_child(box)

        def responded(response: str) -> None:
            if response == "send" and self._selected:
                self.model.pick(self._selected)
                if self.model.state()["phase"] == view.PICKING:
                    # Refused while "Send to…" is sending: ask again.
                    self._show_picker({**self.model.state(), "selected": self._selected})
            else:
                self.model.cancel()

        self._present(dialog, view.PICKING, responded)

    def _choice_toggled(self, button: Gtk.CheckButton, key: str) -> None:
        if button.get_active():
            self._selected = key

    def _show_no_target(self, state: dict) -> None:
        dialog = Adw.AlertDialog(heading=state["title"], body=state["body"])
        dialog.add_response("close", _("Close"))
        dialog.add_response("settings", view.settings_label())
        dialog.set_default_response("settings")
        dialog.set_close_response("close")

        def responded(response: str) -> None:
            self.model.cancel()
            if response == "settings":
                self._open_plugin_settings()

        self._present(dialog, view.NO_TARGET, responded)
