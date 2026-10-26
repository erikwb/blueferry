"""The GTK settings dialog on a private Broadway display: categories, feature
switches with the restart notice and the old-daemon fallback, the doctor."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize(
    "scenario", [
        "categories", "features", "old-daemon", "doctor", "mirror-removals",
        "battery-warning", "reconnect", "notification-rules",
    ],
)
def test_gtk_settings(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


ITEMS = {
    "show_notification_content": {
        "value": True, "running": True, "source": "environment",
        "variable": "BLUEFERRY_SHOW_NOTIFICATION_CONTENT", "restart_required": False,
    },
    "mark_read_on_dismiss": {
        "value": False, "running": False, "source": "default",
        "variable": "BLUEFERRY_MARK_READ_ON_DISMISS", "restart_required": False,
    },
    "notification_history": {
        "value": False, "running": False, "source": "default",
        "variable": "BLUEFERRY_NOTIFICATION_HISTORY", "restart_required": False,
    },
}


class FakeClient:
    """Queues every daemon call; the scenario answers them when it chooses."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    def connect(self, *_args):
        return 0

    def record_status(self, _status):
        pass

    def get_status_async(self, on_ok, _on_err=None):
        on_ok(self.status)

    def unlock_storage_async(self, *_args):
        pass

    def features_async(self, on_ok, on_err=None):
        self.calls.append(("features", on_ok, on_err))

    def set_feature_async(self, name, enabled, on_ok, on_err):
        self.calls.append((("set_feature", name, enabled), on_ok, on_err))

    def restart_backend_async(self, on_ok, on_err):
        self.calls.append(("restart", on_ok, on_err))

    def set_mirror_notification_removals_async(self, enabled, on_ok, on_err):
        self.calls.append((("mirror", enabled), on_ok, on_err))

    def set_phone_battery_warning_async(self, enabled, on_ok, on_err):
        self.calls.append((("battery", enabled), on_ok, on_err))

    def reconnect_phone_async(self, on_ok, on_err):
        self.calls.append(("reconnect", on_ok, on_err))

    def notification_open_map_async(self, on_ok, on_err=None):
        self.calls.append(("open_map", on_ok, on_err))

    def set_notification_open_target_async(self, bundle, target, on_ok, on_err):
        self.calls.append((("set_rule", bundle, target), on_ok, on_err))

    def remove_notification_open_target_async(self, bundle, on_ok, on_err):
        self.calls.append((("remove_rule", bundle), on_ok, on_err))

    def run_doctor_async(self, on_ok, on_err, runner=None):
        self.calls.append(("doctor", on_ok, on_err))

    def take(self, name):
        names = [call[0] for call in self.calls]
        assert name in names, (name, names)
        return self.calls.pop(names.index(name))


def _settings(status=None, **options):
    from types import SimpleNamespace
    from unittest.mock import patch

    from blueferry.models import BackendStatus
    from blueferry.setup_client import ConfigurationState
    from blueferry.ui import status as status_module

    status_module.Adw.init()
    settings = status_module.Gtk.Settings.get_default()
    settings.set_property("gtk-enable-animations", False)
    configuration = ConfigurationState.from_dict({"configured": True, "saved": True})
    client = FakeClient(status or BackendStatus(daemon=True))
    with (
        patch.object(status_module, "SetupClient",
                     lambda: SimpleNamespace(configuration=lambda: configuration)),
        patch.object(status_module.SettingsDialog, "_load_setup_state", lambda self: None),
    ):
        dialog = status_module.SettingsDialog(client, **options)
    window = status_module.Adw.ApplicationWindow(default_width=760, default_height=720)
    window.present()
    return dialog, window, client


def _settle():
    from gi.repository import GLib

    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def _wait_for(condition, timeout=3.0):
    """Run the main loop until ``condition()``."""
    import time

    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        context.iteration(False) or time.sleep(0.01)


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _group_titles(page):
    from gi.repository import Adw

    return [
        widget.get_title() for widget in _descendants(page)
        if isinstance(widget, Adw.PreferencesGroup) and widget.get_title()
    ]


def _exercise_categories():
    from blueferry.ui.settings_presenter import categories

    dialog, window, client = _settings()
    assert list(dialog.pages) == [category.key for category in categories()]
    assert [page.get_title() for page in dialog.pages.values()] == [
        "Phone", "Notifications", "Calls & Audio", "Security", "About"]
    for key, groups in {
        "phone": ["Connection Health", "Diagnostics", "Contacts"],
        "notifications": ["Desktop Notifications", "Content and Actions", "History"],
        "calls": ["Phone Calls", "Call History", "iPhone Sound", "Media Control"],
        "security": ["One-Time Codes", "Away Lock", "Local Data"],
    }.items():
        titles = _group_titles(dialog.pages[key])
        assert [title for title in titles if title in groups] == groups, (key, titles)
    # The pairing report row now belongs to Diagnostics, as in the Qt client.
    assert dialog._issue_row.get_parent() is not None
    assert dialog._issue_row.get_ancestor(type(dialog.doctor.group)) is dialog.doctor.group
    # Opening on a category shows that page and asks for fresh switches.
    dialog.open(window, "calls")
    _settle()
    assert dialog.get_visible_page_name() == "calls"
    assert [call[0] for call in client.calls] == ["features"]
    # No restart is pending: no page shows the notice.
    notices = [group for group, _row in dialog.features._notices]
    assert len(notices) == len(dialog.pages) and not any(g.get_visible() for g in notices)
    assert dialog._service_row.get_subtitle() == "Running"
    dialog.close()
    _settle()
    window.destroy()


def _exercise_features():
    from tests.client_fakes import ManualTimer

    timers = []

    def timer(callback):
        timers.append(ManualTimer(callback))
        return timers[-1]

    dialog, window, client = _settings(timer=timer)
    dialog.open(window, "notifications")
    _settle()
    rows = {name: row for name, (row, _description) in dialog.features._rows.items()}
    assert set(rows) == set(ITEMS)
    # Until the daemon answers, nothing can be switched.
    assert not any(row.get_sensitive() for row in rows.values())
    client.take("features")[1](ITEMS)
    pinned, mark_read = rows["show_notification_content"], rows["mark_read_on_dismiss"]
    # One the service environment sets cannot be changed here.
    assert pinned.get_active() and not pinned.get_sensitive()
    assert "environment (BLUEFERRY_SHOW_NOTIFICATION_CONTENT)" in pinned.get_subtitle()
    assert mark_read.get_sensitive() and not mark_read.get_active()

    mark_read.set_active(True)
    assert mark_read.get_active() and not mark_read.get_sensitive()
    _call, saved, _failed = client.take(("set_feature", "mark_read_on_dismiss", True))
    saved("restart-required")
    # Saved, the reload not answered yet: the switch keeps the choice.
    assert mark_read.get_active() and mark_read.get_sensitive()
    client.take("features")[1]({**ITEMS, "mark_read_on_dismiss": {
        **ITEMS["mark_read_on_dismiss"], "value": True, "source": "settings",
        "restart_required": True,
    }})
    assert mark_read.get_active() and mark_read.get_sensitive()
    assert "Applies after the service restarts." in mark_read.get_subtitle()
    # Every page leads with the notice, worded by SetFeature's answer.
    for group, row in dialog.features._notices:
        assert group.get_visible()
        assert row.get_title() == "Restart the BlueFerry service to apply the change."

    # A failed save puts the switch back and says why.
    toasts = []
    dialog.features._toast = toasts.append
    history = rows["notification_history"]
    history.set_active(True)
    client.take(("set_feature", "notification_history", True))[2]("Access denied")
    assert not history.get_active() and history.get_sensitive()
    assert toasts == ["Could not save the setting: Access denied"]

    # Restart Service: every restart button waits for the restart.
    button = dialog.features._restart_buttons[0]
    button.emit("clicked")
    assert not any(b.get_sensitive() for b in dialog.features._restart_buttons)
    button.emit("clicked")
    assert [call[0] for call in client.calls] == ["restart"]
    client.take("restart")[1](None)
    assert all(b.get_sensitive() for b in dialog.features._restart_buttons)
    status_timer, features_timer = timers
    assert (status_timer.delay, features_timer.delay) == (800, 1200)
    features_timer.fire()
    client.take("features")[1](ITEMS)
    assert not any(group.get_visible() for group, _row in dialog.features._notices)
    # The daemon going away makes the switches insensitive again.
    dialog._apply_status(type(dialog._last_status)(daemon=False))
    assert not mark_read.get_sensitive()
    # Closing the dialog cancels the pending reload.
    dialog._apply_status(type(dialog._last_status)(daemon=True))
    button.emit("clicked")
    client.take("restart")[1](None)
    assert status_timer.active
    dialog.close()
    _settle()
    assert not status_timer.active and not features_timer.active
    # A restart that ends after the dialog closed arms nothing.
    dialog.features.restart()
    client.take("restart")[1](None)
    assert not status_timer.active and not features_timer.active

    # With the real GLib timers, closing removes the armed sources.
    dialog, window, client = _settings()
    dialog.open(window)
    _settle()
    dialog.features.restart()
    client.take("restart")[1](None)
    assert dialog.features._status_timer.active
    dialog.close()
    _settle()
    assert not dialog.features._status_timer.active
    assert not dialog.features._features_timer.active
    window.destroy()


def _exercise_old_daemon():
    dialog, window, client = _settings()
    dialog.open(window, "notifications")
    _settle()
    client.take("features")[2]("No such method 'GetFeatures'")
    for name, (row, _description) in dialog.features._rows.items():
        variable = ITEMS[name]["variable"]
        assert not row.get_sensitive() and not row.get_active()
        assert f"Set {variable}=true or false in ~/.config/blueferry/local.env" in (
            row.get_subtitle()), row.get_subtitle()
    assert not any(group.get_visible() for group, _row in dialog.features._notices)
    dialog.close()
    _settle()
    window.destroy()


def _exercise_doctor():
    from blueferry.doctor_report import DoctorReport

    dialog, window, client = _settings()
    doctor = dialog.doctor
    assert not doctor.report_row.get_visible()
    # A run that ends while Settings is closed does not pop up a dialog.
    doctor.run_button.emit("clicked")
    assert doctor.spinner.get_visible() and not doctor.run_button.get_sensitive()
    doctor.run()
    _name, done, _failed = client.take("doctor")
    assert [call[0] for call in client.calls] == []
    done(DoctorReport(False, False, "ERROR <b>bluetoothd</b> & obexd"))
    assert doctor.report_dialog is None and doctor.report_row.get_visible()
    assert doctor.report_row.get_title() == "One or more checks failed."
    assert doctor.run_button.get_sensitive() and not doctor.spinner.get_visible()

    dialog.open(window, "phone")
    _settle()
    # Activating the row clicks its button.
    assert doctor.run_row.get_activatable_widget() is doctor.run_button
    doctor.run_button.emit("clicked")
    client.take("doctor")[1](DoctorReport(True, True, "WARNING doctor: <b>Adapter</b> CoD"))
    _settle()
    report = doctor.report_dialog
    assert report is not None and report.get_mapped()
    assert report.summary.get_text() == "Checks completed with warnings."
    buffer = report.report.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
    assert text == "WARNING doctor: <b>Adapter</b> CoD"  # plain text, no markup
    report.copy_button.emit("clicked")
    copied = []
    report.get_clipboard().read_text_async(
        None, lambda clipboard, result: copied.append(clipboard.read_text_finish(result)),
    )
    _wait_for(lambda: copied)
    assert copied == [text]
    report.close()
    _settle()
    assert doctor.report_dialog is None
    # The row reopens the last report; a failure shows its message.
    doctor.run()
    client.take("doctor")[2]("blueferry doctor did not finish: TimeoutExpired")
    _settle()
    assert doctor.report_dialog.summary.get_text() == "One or more checks failed."
    doctor.report_dialog.close()
    _settle()
    doctor.report_row.emit("activated")
    _settle()
    assert doctor.report_dialog is not None and doctor.report_dialog.get_mapped()
    doctor.report_dialog.close()
    dialog.close()
    _settle()
    window.destroy()


def _exercise_mirror_removals():
    from blueferry.models import BackendStatus

    dialog, window, client = _settings()
    row = dialog._mirror_removals_row
    assert not row.get_visible()  # older daemons do not report the preference
    status = BackendStatus.from_dict({"daemon": True, "mirror_iphone_removals": False})
    dialog._apply_status(status)
    assert row.get_visible() and not row.get_active() and row.get_sensitive()
    row.set_active(True)
    assert not row.get_sensitive()
    _call, _saved, failed = client.take(("mirror", True))
    failed("busy")
    # A failed save shows the daemon's value again.
    assert not row.get_active() and row.get_sensitive()
    client.status = BackendStatus.from_dict({"daemon": True, "mirror_iphone_removals": True})
    row.set_active(True)
    client.take(("mirror", True))[1](True)
    assert row.get_active() and row.get_sensitive()
    window.destroy()


def _exercise_battery_warning():
    from blueferry.models import BackendStatus

    dialog, window, client = _settings()
    group, row = dialog._battery_group, dialog._battery_warning_row
    assert group.get_ancestor(type(dialog.pages["calls"])) is dialog.pages["calls"]
    dialog._apply_status(BackendStatus.from_dict({"daemon": True}))  # an older daemon
    assert not group.get_visible()
    dialog._apply_status(BackendStatus.from_dict({
        "daemon": True, "phone_battery_warning": False, "phone_battery_warning_percent": 15,
    }))
    assert group.get_visible() and not row.get_active() and row.get_sensitive()
    assert row.get_subtitle() == "One desktop notification per discharge, at 15 % or less."
    toasts = []
    dialog._toast = toasts.append
    row.set_active(True)
    assert not row.get_sensitive()
    client.take(("battery", True))[2]("busy")
    assert not row.get_active() and row.get_sensitive()
    assert toasts == ["Could not save battery warning preference: busy"]
    client.status = BackendStatus.from_dict({
        "daemon": True, "phone_battery_warning": True, "phone_battery_warning_percent": 20,
    })
    row.set_active(True)
    client.take(("battery", True))[1](True)
    assert row.get_active() and row.get_sensitive()
    assert row.get_subtitle().endswith("at 20 % or less.")
    dialog._apply_status(BackendStatus.from_dict({"daemon": False}))
    assert not row.get_sensitive()
    window.destroy()


def _exercise_reconnect():
    from blueferry.models import BackendStatus
    from blueferry.reconnect_view import STARTED_TEXT, UNREACHABLE_TEXT

    dialog, window, client = _settings()
    row, button = dialog._reconnect_row, dialog._reconnect_button
    assert not row.get_visible()  # older daemons have no reconnect state
    toasts = []
    dialog._toast = toasts.append
    waiting = {"daemon": True, "phone_reconnect_state": "waiting"}
    dialog._apply_status(BackendStatus.from_dict(waiting))
    assert row.get_visible() and button.get_visible()
    assert row.get_subtitle() == "Reconnecting automatically."
    button.emit("clicked")
    assert not button.get_sensitive()
    client.status = BackendStatus.from_dict({**waiting, "phone_reconnect_state": "connecting"})
    client.take("reconnect")[1]("started")
    assert button.get_sensitive() and row.get_subtitle() == STARTED_TEXT
    # Re-rendering the old status (a failed save elsewhere) keeps the notice.
    dialog._apply_status(dialog._last_status)
    assert row.get_subtitle() == STARTED_TEXT
    # The outcome ends it; an unreachable phone is reported.
    dialog._apply_status(BackendStatus.from_dict(
        {**waiting, "phone_reconnect_state": "unreachable"}))
    assert toasts == [UNREACHABLE_TEXT]
    assert row.get_subtitle() == "Reconnecting automatically."
    button.emit("clicked")
    client.take("reconnect")[1]("unreachable")
    assert toasts[-1] == UNREACHABLE_TEXT
    button.emit("clicked")
    client.take("reconnect")[2]("Too many requests")
    assert toasts[-1] == "Too many requests" and button.get_sensitive()
    # Connected: nothing to offer; a hung bluetoothd hides the button.
    dialog._apply_status(BackendStatus.from_dict(
        {**waiting, "phone_reconnect_state": "connected"}))
    assert row.get_visible() and not button.get_visible()
    dialog._apply_status(BackendStatus.from_dict({**waiting, "bluez_unresponsive": True}))
    assert not button.get_visible() and "not responding" in row.get_subtitle()
    window.destroy()


def _exercise_notification_rules():
    from blueferry.models import BackendStatus

    dialog, window, client = _settings()
    rules = dialog.notification_rules
    page = dialog.pages["notifications"]
    assert rules.group.get_ancestor(type(page)) is page
    assert not rules.group.get_visible() and not rules.form.get_visible()

    def status(**values):
        return BackendStatus.from_dict({"daemon": True, "notification_policy": "all",
                                        "notification_open_map_revision": 1, **values})

    # Messages only, or a daemon without the revision: no editor, no read.
    dialog._apply_status(status(notification_policy="messages"))
    dialog._apply_status(BackendStatus.from_dict({"daemon": True,
                                                  "notification_policy": "all"}))
    assert not rules.group.get_visible() and client.calls == []
    dialog._apply_status(status())
    assert rules.group.get_visible() and rules.form.get_visible()
    client.take("open_map")[1]([])
    assert rules.empty_row.get_visible()
    # The same revision again is not reread.
    dialog._apply_status(status())
    assert client.calls == []

    assert not rules.add_button.get_sensitive()
    rules.bundle_entry.set_text("com.apple.mobilemail")
    rules.target_entry.set_text("javascript:alert(1)")
    assert not rules.add_button.get_sensitive()
    assert rules.target_entry.has_css_class("error")
    rules.target_entry.set_text(" https://mail.example.com/ ")
    assert rules.add_button.get_sensitive() and not rules.target_entry.has_css_class("error")
    rules.target_entry.emit("entry-activated")  # Enter in the target field adds
    _name, updated, failed = client.take(
        ("set_rule", "com.apple.mobilemail", "https://mail.example.com/"))
    # A failure keeps the form; an answer without this rule does too.
    failed("invalid target")
    updated([{"bundle_id": "org.other", "target": "https://other.example/"}])
    assert rules.bundle_entry.get_text() == "com.apple.mobilemail"
    rule = {"bundle_id": "com.apple.mobilemail", "target": "https://mail.example.com/"}
    updated([rule])
    assert rules.bundle_entry.get_text() == "" and rules.target_entry.get_text() == ""
    assert not rules.empty_row.get_visible()
    rows = [row for row in _descendants(rules.group)
            if getattr(row, "get_title", None) and row.get_title() == "com.apple.mobilemail"]
    assert len(rows) == 1 and rows[0].get_subtitle() == "https://mail.example.com/"
    remove = next(widget for widget in _descendants(rows[0])
                  if getattr(widget, "get_icon_name", None)
                  and widget.get_icon_name() == "edit-delete-symbolic")
    remove.emit("clicked")
    client.take(("remove_rule", "com.apple.mobilemail"))[1](True)
    client.take("open_map")[1]([])
    assert rules.empty_row.get_visible() and rules.rules == []
    # Another client changed the rules: a new revision rereads them.
    dialog._apply_status(status(notification_open_map_revision=2))
    client.take("open_map")
    window.destroy()


if __name__ == "__main__":
    {
        "notification-rules": _exercise_notification_rules,
        "reconnect": _exercise_reconnect,
        "battery-warning": _exercise_battery_warning,
        "mirror-removals": _exercise_mirror_removals,
        "categories": _exercise_categories,
        "features": _exercise_features,
        "old-daemon": _exercise_old_daemon,
        "doctor": _exercise_doctor,
    }[sys.argv[1]]()
