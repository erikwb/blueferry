"""Persistent popup policy is validated and written privately."""
from __future__ import annotations

import json
import stat

import pytest

from blueferry.notification_policy import NotificationPolicyStore


def test_default_is_messages_only(tmp_path) -> None:
    store = NotificationPolicyStore(tmp_path / "blueferry" / "settings.json")

    assert store.value == "messages"
    assert store.contacts_only is False


def test_policy_persists_and_preserves_future_settings(tmp_path) -> None:
    path = tmp_path / "blueferry" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"future_setting": 7}))
    store = NotificationPolicyStore(path)

    assert store.set("ALL") == "all"
    assert store.set_contacts_only(True) is True

    assert NotificationPolicyStore(path).value == "all"
    assert NotificationPolicyStore(path).contacts_only is True
    assert json.loads(path.read_text()) == {
        "future_setting": 7,
        "desktop_notifications": "all",
        "contacts_only_notifications": True,
    }
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_policy_does_not_replace_existing_value(tmp_path) -> None:
    path = tmp_path / "blueferry" / "settings.json"
    store = NotificationPolicyStore(path)
    store.set("none")

    with pytest.raises(ValueError, match="all, messages, none"):
        store.set("sometimes")

    assert store.value == "none"
    assert NotificationPolicyStore(path).value == "none"


def test_contacts_only_requires_a_real_boolean(tmp_path) -> None:
    store = NotificationPolicyStore(tmp_path / "settings.json")

    with pytest.raises(ValueError, match="must be a boolean"):
        store.set_contacts_only(1)  # type: ignore[arg-type]

    assert store.contacts_only is False
