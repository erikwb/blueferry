"""Pure presentation rules for the GTK settings dialog (no display needed).

The categories follow the Qt client's settings page (``qt/qml/SettingsPage.qml``)
in the same order, each one an ``Adw.PreferencesPage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blueferry import doctor_report
from blueferry.i18n import _


@dataclass(frozen=True, slots=True)
class Category:
    key: str
    title: str
    icon: str


def categories() -> tuple[Category, ...]:
    return (
        Category("phone", _("Phone"), "phone-symbolic"),
        Category("notifications", _("Notifications"), "preferences-system-notifications-symbolic"),
        Category("calls", _("Calls & Audio"), "call-start-symbolic"),
        Category("security", _("Security"), "security-high-symbolic"),
        Category("plugins", _("Plugins"), "application-x-addon-symbolic"),
        Category("about", _("About"), "help-about-symbolic"),
    )


@dataclass(frozen=True, slots=True)
class DoctorRows:
    """The Diagnostics group: the run row and the last report's row."""

    running: bool
    run_subtitle: str
    report_shown: bool
    report_title: str = ""
    report_icon: str = ""
    report_css: str = ""


_LEVEL_STYLE = {
    "ok": ("object-select-symbolic", "success"),
    "warning": ("dialog-warning-symbolic", "warning"),
    "error": ("dialog-error-symbolic", "error"),
}


def doctor_rows(state: dict[str, Any]) -> DoctorRows:
    running = state.get("running") is True
    subtitle = _("Running checks…") if running else ""
    if state.get("ran") is not True:
        return DoctorRows(running, subtitle, False)
    view = doctor_report.view(state)
    icon, css = _LEVEL_STYLE[view["level"]]
    return DoctorRows(running, subtitle, True, view["summary"], icon, css)


def feature_subtitle(description: str, note: str) -> str:
    """A switch row's subtitle: its description, then the source note."""
    return "\n".join(part for part in (description, note) if part)


def service_state(daemon: bool) -> str:
    return _("Running") if daemon else _("Unavailable")
