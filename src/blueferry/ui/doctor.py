"""Settings > Phone > Diagnostics: ``blueferry doctor`` inside the GTK client.

The check runs in a child process on the client's local worker
(:meth:`DaemonClient.run_doctor_async`), never on the GTK thread. Its state
comes from :mod:`blueferry.doctor_report`, shared with the Qt client. The
report is local diagnostics for the user's own screen (it names the
configured MAC address), shown as plain text with a Copy button.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gdk, Gtk

from blueferry import doctor_report
from blueferry.i18n import _
from blueferry.ui.settings_presenter import doctor_rows


class DoctorReportDialog(Adw.Dialog):
    """The last report in a read-only monospace view with Copy."""

    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__(
            title=_("Diagnostics Report"), content_width=640, content_height=520,
        )
        view = doctor_report.view(state)
        self.text = view["text"]
        self._toasts = Adw.ToastOverlay()
        header = Adw.HeaderBar()
        self.copy_button = Gtk.Button(
            icon_name="edit-copy-symbolic", tooltip_text=_("Copy Report"),
        )
        self.copy_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Copy Report")])
        self.copy_button.connect("clicked", lambda _button: self.copy())
        header.pack_start(self.copy_button)

        self.summary = Gtk.Label(
            label=view["summary"], wrap=True, xalign=0, css_classes=["heading"],
        )
        self.summary.add_css_class(doctor_rows(state).report_css)
        self.summary.set_margin_start(12)
        self.summary.set_margin_end(12)
        self.summary.set_margin_top(6)
        self.summary.set_margin_bottom(6)
        buffer = Gtk.TextBuffer()
        buffer.set_text(self.text)
        self.report = Gtk.TextView(
            buffer=buffer, editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=12, bottom_margin=12, left_margin=12, right_margin=12,
        )
        self.report.update_property([Gtk.AccessibleProperty.LABEL], [_("Diagnostics Report")])
        scrolled = Gtk.ScrolledWindow(child=self.report, vexpand=True)
        scrolled.add_css_class("card")
        scrolled.set_margin_start(12)
        scrolled.set_margin_end(12)
        scrolled.set_margin_bottom(12)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.append(self.summary)
        content.append(scrolled)
        self._toasts.set_child(content)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._toasts)
        self.set_child(toolbar)

    def copy(self) -> None:
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(self.text))
        self._toasts.add_toast(Adw.Toast(title=_("Report copied"), use_markup=False))


class DoctorSection:
    """The Diagnostics group: run the check, open the last report."""

    def __init__(self, client, parent: Gtk.Widget, runner: Callable | None = None) -> None:
        self._client = client
        self._parent = parent
        self._runner = runner
        self._state: dict[str, Any] = doctor_report.idle_state()
        self.report_dialog: DoctorReportDialog | None = None
        self.group = Adw.PreferencesGroup(
            title=_("Diagnostics"),
            description=_(
                "Checks the Bluetooth controller, the BlueZ OBEX service, the "
                "configured iPhone and the notification link, like the command "
                "blueferry doctor."
            ),
        )
        self.run_row = Adw.ActionRow(title=_("Run Diagnostics"), use_markup=False)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.run_row.add_suffix(self.spinner)
        self.run_button = Gtk.Button(label=_("Run"), valign=Gtk.Align.CENTER)
        self.run_button.connect("clicked", lambda _button: self.run())
        self.run_row.add_suffix(self.run_button)
        self.run_row.set_activatable_widget(self.run_button)
        self.group.add(self.run_row)
        self.report_row = Adw.ActionRow(
            title="", subtitle=_("Show the full report"), use_markup=False, activatable=True,
        )
        self._report_icon = Gtk.Image()
        self.report_row.add_prefix(self._report_icon)
        self.report_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.report_row.connect("activated", lambda _row: self.show_report())
        self.group.add(self.report_row)
        self._render()

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def run(self) -> None:
        if self._state["running"]:
            return
        self._state = doctor_report.running_state(self._state)
        self._render()

        def done(report: object) -> None:
            self._state = doctor_report.finished_state(self._state, report)
            self._finished()

        def failed(message: str) -> None:
            self._state = doctor_report.failed_state(message)
            self._finished()

        if self._runner is None:
            self._client.run_doctor_async(done, failed)
        else:
            self._client.run_doctor_async(done, failed, runner=self._runner)

    def show_report(self) -> None:
        if self._state["ran"] is not True:
            return
        if self.report_dialog is not None:
            self.report_dialog.force_close()
        dialog = DoctorReportDialog(self._state)
        dialog.connect("closed", self._report_closed)
        self.report_dialog = dialog
        dialog.present(self._parent)

    def _report_closed(self, dialog: DoctorReportDialog) -> None:
        if self.report_dialog is dialog:
            self.report_dialog = None

    def _finished(self) -> None:
        self._render()
        # A run that ends after Settings closed waits for the next visit.
        if self._parent.get_mapped():
            self.show_report()

    def _render(self) -> None:
        rows = doctor_rows(self._state)
        self.spinner.set_visible(rows.running)
        self.spinner.set_spinning(rows.running)
        self.run_button.set_sensitive(not rows.running)
        self.run_row.set_subtitle(rows.run_subtitle)
        self.report_row.set_visible(rows.report_shown)
        self.report_row.set_title(rows.report_title)
        self._report_icon.set_from_icon_name(rows.report_icon or None)
        for css in ("success", "warning", "error"):
            self._report_icon.remove_css_class(css)
        if rows.report_css:
            self._report_icon.add_css_class(rows.report_css)
