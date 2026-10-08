"""Notification click targets are strictly validated user configuration."""
from __future__ import annotations

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blueferry.limits import MAX_NOTIFICATION_OPEN_MAPPINGS
from blueferry.notification_open_map import (
    DESKTOP_TARGET,
    URL_TARGET,
    OpenTarget,
    normalize_open_map,
    open_map_entries,
    parse_target,
    resolve_open_target,
    validate_bundle_id,
)


@pytest.mark.parametrize("value", [
    "https://web.whatsapp.com",
    "https://web.whatsapp.com/",
    "http://localhost:8080/calendar",
    "https://calendar.google.com/calendar/u/0/r?tab=mc&authuser=0",
    "https://example.com/path;v=1/(x)*!,$+=",
    "https://example.com/a%20b#frag",
    "HTTPS://Example.COM/",
    "https://[::1]:8443/",
    "https://192.0.2.1/",
    "https://exa_mple.com/",
    "https://_dmarc.example.com/",
    "https://localhost/",
])
def test_http_urls_are_accepted_unchanged(value) -> None:
    assert parse_target(value) == OpenTarget(URL_TARGET, value)


@pytest.mark.parametrize("value", [
    "org.mozilla.Thunderbird.desktop",
    "thunderbird.desktop",
    "com.slack.Slack.desktop",
    "kde-org.kde.kontact.desktop",
    "google-chrome.desktop",
    "org_example.App_1.desktop",
])
def test_desktop_entry_ids_are_accepted(value) -> None:
    assert parse_target(value) == OpenTarget(DESKTOP_TARGET, value)


@pytest.mark.parametrize("value", [
    # Non-web schemes, including ones that execute or read local data.
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "file:///etc/passwd",
    "file://localhost/home/user/.ssh/id_ed25519",
    "data:text/html,<script>alert(1)</script>",
    "ftp://example.com/",
    "mailto:someone@example.com",
    "slack://open",
    "whatsapp://send",
    "vbscript:msgbox",
    "https:example.com",
    "https:/example.com",
    "https:///path",
    "//example.com/path",
    # Credentials, spaces, controls, non-ASCII and malformed escapes.
    "https://user:pass@example.com/",
    "https://@example.com/",
    "https://example.com/a b",
    "https://example.com/a\nb",
    "https://exa\tmple.com/",
    "https://example.com/a\x00b",
    "https://exämple.com/",
    "https://example.com/%zz",
    "https://example.com/%",
    "https://example.com:99999/",
    "https://example.com:port/",
    "https://x..com/",
    "https://.example.com/",
    "https://example.com./",
    "https://example.com:/",
    "https://a..bc/",
    "https://[::1/",
    # Characters that must be escaped, including shell/markup metacharacters.
    'https://example.com/"',
    "https://example.com/'",
    "https://example.com/`id`",
    "https://example.com/<b>",
    "https://example.com/{title}",
    "https://example.com/a|b",
    "https://example.com/\\",
    "https://example.com/^",
    "https://" + "a" * 2048 + ".com/",
    # Commands and paths are never targets.
    "thunderbird",
    "/usr/bin/thunderbird",
    "/usr/share/applications/thunderbird.desktop",
    "./thunderbird.desktop",
    "../thunderbird.desktop",
    "~/thunderbird.desktop",
    "applications/thunderbird.desktop",
    ".desktop",
    ".hidden.desktop",
    "-rf.desktop",
    "thunderbird.desktop; rm -rf ~",
    "thunderbird.desktop && id",
    "$(id).desktop",
    "`id`.desktop",
    "thunder bird.desktop",
    "thunder\nbird.desktop",
    "thunderbird..desktop",
    "thunderbird.desktop %u",
    "sh -c 'xdg-open https://example.com'",
    "xdg-open https://example.com",
    "",
    "   ",
])
def test_unsafe_or_ambiguous_targets_are_rejected(value) -> None:
    with pytest.raises(ValueError):
        parse_target(value)


@pytest.mark.parametrize("value", [None, 1, ["https://example.com"], b"https://example.com"])
def test_non_string_targets_are_rejected(value) -> None:
    with pytest.raises(ValueError):
        parse_target(value)


def test_surrounding_whitespace_is_trimmed() -> None:
    assert parse_target("  https://example.com/  ").value == "https://example.com/"
    assert validate_bundle_id(" com.apple.mobilemail ") == "com.apple.mobilemail"


@pytest.mark.parametrize("value", [
    "com.apple.mobilemail",
    "net.whatsapp.WhatsApp",
    "com.tinyspeck.chatlyio",
    "com.google.calendar",
    "com.example.my-app_2",
])
def test_bundle_ids_are_accepted(value) -> None:
    assert validate_bundle_id(value) == value


@pytest.mark.parametrize("value", [
    "",
    "mail",
    ".com.example",
    "com..example",
    "com.example.",
    "-com.example",
    "com.example/App",
    "com.example App",
    "com.example;id",
    "com.exämple.app",
    "com." + "a" * 255,
    "com.apple.MobileSMS",
    "com.apple.mobilesms",
    "COM.APPLE.MOBILESMS",
    None,
    7,
])
def test_invalid_or_messages_bundle_ids_are_rejected(value) -> None:
    with pytest.raises(ValueError):
        validate_bundle_id(value)


def test_resolution_is_exact_and_revalidates_stored_values() -> None:
    mapping = {
        "com.apple.mobilemail": "org.mozilla.Thunderbird.desktop",
        "net.whatsapp.WhatsApp": "https://web.whatsapp.com",
        # A hand-edited value that bypassed the store.
        "com.example.Evil": "file:///etc/passwd",
        "com.apple.MobileSMS": "https://example.com",
    }

    assert resolve_open_target(mapping, "com.apple.mobilemail") == OpenTarget(
        DESKTOP_TARGET, "org.mozilla.Thunderbird.desktop"
    )
    assert resolve_open_target(mapping, "net.whatsapp.WhatsApp") == OpenTarget(
        URL_TARGET, "https://web.whatsapp.com"
    )
    assert resolve_open_target(mapping, "COM.APPLE.MOBILEMAIL") is None
    assert resolve_open_target(mapping, "com.apple.mobilemail ") is None
    assert resolve_open_target(mapping, "com.example.Evil") is None
    assert resolve_open_target(mapping, "com.apple.MobileSMS") is None
    assert resolve_open_target({"com.apple.mobilesms": "https://example.com"}, "com.apple.mobilesms") is None
    assert resolve_open_target(mapping, "com.example.Unmapped") is None
    assert resolve_open_target(mapping, None) is None


def test_stored_map_keeps_only_valid_rules_up_to_the_limit() -> None:
    raw = {
        "com.apple.mobilemail": "org.mozilla.Thunderbird.desktop",
        "bad id": "https://example.com",
        "com.example.Script": "javascript:alert(1)",
        "com.example.Number": 5,
    }
    assert normalize_open_map(raw) == {
        "com.apple.mobilemail": "org.mozilla.Thunderbird.desktop",
    }
    assert normalize_open_map(["com.apple.mobilemail"]) == {}
    assert normalize_open_map(None) == {}

    many = {f"com.example.app{index}": "https://example.com/" for index in range(200)}
    assert len(normalize_open_map(many)) == MAX_NOTIFICATION_OPEN_MAPPINGS


def test_wire_entries_are_sorted_and_typed() -> None:
    assert open_map_entries({
        "net.whatsapp.WhatsApp": "https://web.whatsapp.com",
        "com.apple.mobilemail": "org.mozilla.Thunderbird.desktop",
    }) == [
        {
            "bundle_id": "com.apple.mobilemail",
            "target": "org.mozilla.Thunderbird.desktop",
            "kind": "desktop",
        },
        {
            "bundle_id": "net.whatsapp.WhatsApp",
            "target": "https://web.whatsapp.com",
            "kind": "url",
        },
    ]


_FORBIDDEN = set(" \t\r\n\"'`<>\\{}|^")


@settings(max_examples=400, deadline=None, derandomize=True)
@given(st.text(max_size=80))
def test_any_accepted_target_is_web_url_or_bare_desktop_id(value) -> None:
    try:
        target = parse_target(value)
    except ValueError:
        return
    assert target.value == value.strip()
    assert not (_FORBIDDEN & set(target.value))
    assert all(0x20 < ord(character) < 0x7F for character in target.value)
    if target.kind == URL_TARGET:
        assert target.value.lower().startswith(("http://", "https://"))
    else:
        assert target.kind == DESKTOP_TARGET
        assert target.value.endswith(".desktop")
        assert "/" not in target.value and not target.value.startswith((".", "-"))


@settings(max_examples=300, deadline=None, derandomize=True)
@given(st.text(alphabet=string.printable, max_size=40))
def test_scheme_prefixed_input_is_never_a_desktop_entry(suffix) -> None:
    for scheme in ("javascript:", "file:", "data:", "vbscript:"):
        with pytest.raises(ValueError):
            parse_target(scheme + suffix)
