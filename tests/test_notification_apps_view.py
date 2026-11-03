"""The toolkit-neutral presenter for the muted-apps settings list."""
from __future__ import annotations

import json

from blueferry import notification_apps_view as view
from tests.test_cli_notification_apps import _Backend


def test_presenter_rows_hint_and_helpers() -> None:
    raw = json.dumps(_Backend().payload)
    rows = view.app_rows(raw)
    assert rows[0] == {
        "app_id": "com.example.Chat", "name": "Chat\x1b[31m", "muted": True, "seen": True,
        "toggle_label": "Unmute", "subtitle": "com.example.Chat · Muted", "enabled": True,
    }
    assert rows[1]["name"] == "com.example.Bare" and rows[1]["toggle_label"] == "Mute"
    assert "since BlueFerry started" in rows[1]["subtitle"]
    assert view.muted_label() == "Muted"
    assert view.muted_ids(raw) == frozenset({"com.example.Chat"})
    assert view.is_muted("com.example.Chat", raw) and not view.is_muted("com.example.Bare", raw)
    assert "BLOCKLIST" in view.apps_hint(raw)
    assert view.toggle_at(raw, 0) == ("com.example.Chat", False)
    assert view.toggle_at(raw, 1) == ("com.example.Bare", True)
    assert view.toggle_at(raw, 2) is None and view.toggle_at(raw, True) is None
    listed = view.list_view(raw)
    assert listed["count"] == 2 and listed["muted_count"] == 1 and listed["available"]
    assert view.supported({"notification_muted_app_count": 0})
    assert not view.supported({})


def test_presenter_hints_for_empty_locked_policy_and_limit() -> None:
    assert view.normalize("not json")["apps"] == []
    empty = {"apps": [], "mute_available": True, "all_notifications": True}
    assert "No iPhone app" in view.apps_hint(empty)
    policy = {"apps": [], "mute_available": True, "all_notifications": False}
    assert "All iPhone Notifications" in view.apps_hint(policy)
    locked = {"apps": [{"app_id": "com.example.A", "muted": False}], "all_notifications": True}
    assert "locked" in view.apps_hint(locked)
    assert view.app_rows(locked)[0]["enabled"] is False
    assert view.toggle_at(locked, 0) is None
    full = {
        "apps": [
            {"app_id": "com.example.A", "muted": True},
            {"app_id": "com.example.B", "muted": False},
        ],
        "max_muted": 1, "mute_available": True, "all_notifications": True,
    }
    rows = view.app_rows(full)
    assert rows[0]["enabled"] is True and rows[1]["enabled"] is False
    assert "At most 1" in view.apps_hint(full)
    assert "ALLOWLIST" in view.apps_hint({**full, "env_allowlist": True})
