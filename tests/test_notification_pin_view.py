"""Toolkit-neutral pin labels and error texts for the notification list."""
from __future__ import annotations

from blueferry import notification_pin_view


def test_presenter_row_extras_and_targets() -> None:
    extras = notification_pin_view.pin_row_extras(
        {"key": "k1", "pinned": True, "removed": True, "actionable": False},
    )
    assert extras == {
        "key": "k1", "can_pin": True, "pinned": True, "removed": True,
        "actionable": False, "removed_text": "Removed on iPhone",
        "pinned_text": "Pinned", "pin_label": "Unpin",
        "pin_tooltip": notification_pin_view.UNPIN_TOOLTIP,
    }
    assert notification_pin_view.pin_target({"key": "k1", "pinned": True}) == ("k1", False)
    assert notification_pin_view.pin_target({"key": "k2"}) == ("k2", True)
    old = notification_pin_view.pin_row_extras({"id": 1})  # an older backend
    assert old["can_pin"] is False and old["pin_label"] == "Pin" and old["removed_text"] == ""
    assert notification_pin_view.pin_target("junk") is None


def test_presenter_limit_and_errors() -> None:
    view = notification_pin_view
    snapshot = {"pins_max": 2, "notifications": [{"pinned": True}, {"pinned": True}, {}]}
    assert view.pin_limit_reached(snapshot)
    assert not view.pin_limit_reached({"notifications": [{"pinned": True}]})
    assert "2" in view.pin_limit_text(2) and view.pin_limit_text(None) == ""
    prefix = "io.weirdware.BlueFerry.Error."
    assert view.pin_error_text(prefix + "RateLimited") == view.RATE_LIMITED_TEXT
    assert view.pin_error_text("org.freedesktop.DBus.Error.UnknownMethod") == view.UNSUPPORTED_TEXT
    assert view.pin_error_text(prefix + "NotReady", "the notification list is off; …") == view.OFF_TEXT
    assert view.pin_error_text(prefix + "NotReady", "keyring locked") == view.LOCKED_TEXT
    assert view.pin_error_text(prefix + "InvalidArgs", "unknown notification key; …") == (
        view.UNKNOWN_KEY_TEXT
    )
    assert view.pin_error_text(
        prefix + "InvalidArgs", "at most 20 notifications can be pinned; unpin one first",
    ) == view.FULL_TEXT
    assert view.pin_error_text(prefix + "InvalidArgs", "… too long to store; …") == view.TOO_LONG_TEXT
    assert view.pin_error_text(prefix + "Failed") == view.GENERIC_ERROR_TEXT
