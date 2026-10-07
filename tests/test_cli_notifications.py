"""`blueferry notifications open-map` talks only to a fake backend client."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from blueferry import cli_notifications
from blueferry.cli import app
from blueferry.client import BackendError
from blueferry.notification_open_map import open_map_entries


class _Backend:
    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self.calls: list[tuple] = []

    def notification_open_map(self):
        self.calls.append(("list",))
        return open_map_entries(self.rules)

    def set_notification_open_target(self, bundle_id, target):
        self.calls.append(("set", bundle_id, target))
        self.rules[bundle_id.strip()] = target.strip()
        return open_map_entries(self.rules)

    def remove_notification_open_target(self, bundle_id):
        self.calls.append(("remove", bundle_id))
        return self.rules.pop(bundle_id, None) is not None


@pytest.fixture
def backend(monkeypatch):
    fake = _Backend()
    monkeypatch.setattr(cli_notifications, "_client", lambda: fake)
    monkeypatch.setattr(cli_notifications, "_desktop_entry_installed", lambda _id: True)
    return fake


def _run(*args):
    return CliRunner().invoke(app, ["notifications", "open-map", *args])


def test_list_is_the_default_and_explains_the_empty_state(backend) -> None:
    for args in ((), ("list",)):
        result = _run(*args)
        assert result.exit_code == 0, result.output
        assert "default behaviour" in result.output
    assert backend.calls == [("list",), ("list",)]


def test_set_list_and_remove_round_trip(backend) -> None:
    result = _run("set", "com.apple.mobilemail", "org.mozilla.Thunderbird.desktop")
    assert result.exit_code == 0, result.output
    result = _run("set", "net.whatsapp.WhatsApp", "https://web.whatsapp.com")
    assert result.exit_code == 0, result.output

    listing = _run("list").output.splitlines()
    assert listing[0].split() == ["com.apple.mobilemail", "desktop", "org.mozilla.Thunderbird.desktop"]
    assert listing[1].split() == ["net.whatsapp.WhatsApp", "url", "https://web.whatsapp.com"]

    result = _run("remove", "com.apple.mobilemail")
    assert result.exit_code == 0
    assert "removed" in result.output
    result = _run("remove", "com.apple.mobilemail")
    assert result.exit_code == 1
    assert backend.rules == {"net.whatsapp.WhatsApp": "https://web.whatsapp.com"}


@pytest.mark.parametrize(("bundle_id", "target"), [
    ("com.example.App", "javascript:alert(1)"),
    ("com.example.App", "file:///etc/passwd"),
    ("com.example.App", "thunderbird"),
    ("com.example.App", "/usr/bin/thunderbird"),
    ("com.example.App", "thunderbird.desktop; id"),
    ("com.apple.MobileSMS", "https://example.com"),
    ("not-a-bundle", "https://example.com"),
])
def test_invalid_rules_are_refused_before_reaching_the_backend(backend, bundle_id, target) -> None:
    result = _run("set", bundle_id, target)

    assert result.exit_code == 2
    assert backend.calls == []


def test_backend_errors_exit_nonzero(monkeypatch) -> None:
    class _Failing:
        def notification_open_map(self):
            raise BackendError("daemon unavailable")

    monkeypatch.setattr(cli_notifications, "_client", _Failing)

    result = _run("list")
    assert result.exit_code == 2
    assert "daemon unavailable" in result.output


def test_missing_desktop_entry_is_saved_with_a_warning(backend, monkeypatch) -> None:
    monkeypatch.setattr(cli_notifications, "_desktop_entry_installed", lambda _id: False)

    result = _run("set", "com.slack", "com.slack.Slack.desktop")

    assert result.exit_code == 0
    assert "not installed" in result.output
    assert backend.rules == {"com.slack": "com.slack.Slack.desktop"}


def test_an_unrestarted_older_backend_gets_a_plain_explanation() -> None:
    import json

    import dbus.exceptions

    from blueferry.client import BackendClient, BackendError
    from blueferry.protocol import MESSAGES_API_VERSION

    class _OldBackend:
        def GetStatus(self, timeout=None):
            return json.dumps({"daemon": True, "api_version": MESSAGES_API_VERSION})

        def __getattr__(self, name):
            def missing(*_args, **_kwargs):
                raise dbus.exceptions.DBusException(
                    f"Method {name} doesn't exist",
                    name="org.freedesktop.DBus.Error.UnknownMethod",
                )
            return missing

    client = BackendClient(interface_factory=lambda _interface: _OldBackend())
    for call in (
        client.notification_open_map,
        lambda: client.set_notification_open_target("com.slack", "slack.desktop"),
        lambda: client.remove_notification_open_target("com.slack"),
        lambda: client.open_notification_click("abc", ""),
    ):
        with pytest.raises(BackendError, match="restart it after upgrading"):
            call()
