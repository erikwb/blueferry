"""Plugin API 1.6: buttons in the settings form and named help links."""
from __future__ import annotations

import json

import pytest

from blueferry.plugin_api import API_MINOR, METHOD_RUN_CONFIG_ACTION, PLUGIN_INTERFACE
from blueferry.plugin_api import surfaces as sf
from blueferry.plugin_api.client import PluginClient, PluginError
from blueferry.plugin_api.config import MAX_ACTIONS
from blueferry.plugin_api.manifest import ManifestError
from blueferry.plugin_api.service import PluginService, RateLimitedError
from blueferry.plugin_api.testing import (
    FakeHost,
    ScriptedTransport,
    ServiceTransport,
    inline_service,
    manifest,
)

SETTINGS = (
    "[Config port]\nLabel=Port\nType=int\nDefault=47801\n"
    "HelpUrl=https://example.org/help#port\nHelpUrlLabel=Which port?\n"
)
SETUP = (
    "[ConfigAction setup_iphone]\nLabel=Set up iPhone\nIcon=smartphone\nPrimary=true\n"
    "Help=Shows a QR code that the iPhone scans.\n"
    "[ConfigAction new_token]\nLabel=New token\n"
)


def _manifest(extra: str = SETTINGS + SETUP, version: str = "1.6"):
    return manifest("io.example.setup", capabilities="card;", api_version=version, extra=extra)


class _Setup(PluginService):
    def __init__(self, *args, cache=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache = cache
        self.ran: list[str] = []
        self.port = 47801

    def config_values(self):
        return {"port": self.port}

    def config_action(self, action_id):
        self.ran.append(action_id)
        if action_id == "setup_iphone":
            return sf.ActionResult(True, "Scan the code", "http://127.0.0.1:4711/abc/")
        self.port += 1
        return {"ok": True, "message": "New token\x1b[31m created", "open_uri": "file:///etc/passwd"}


def test_the_minor_is_six_and_a_newer_manifest_is_ignored() -> None:
    assert API_MINOR == 6
    assert _manifest().api_minor == 6
    with pytest.raises(ManifestError, match=r"needs plugin API 1\.7; this BlueFerry supports 1\.6"):
        _manifest(version="1.7")


def test_actions_and_help_link_names_are_parsed() -> None:
    parsed = _manifest()
    assert [(a.id, a.label, a.icon, a.primary) for a in parsed.config_actions] == [
        ("setup_iphone", "Set up iPhone", "smartphone", True),
        ("new_token", "New token", "", False),
    ]
    assert parsed.config_actions[0].help == "Shows a QR code that the iPhone scans."
    assert parsed.config_action("new_token") is parsed.config_actions[1]
    assert parsed.config_action("nope") is None
    assert parsed.config[0].help_url_label == "Which port?"
    # An older manifest has neither; a 1.5 plugin may still name its links.
    old = _manifest(extra="[Config port]\nLabel=Port\nType=int\n", version="1.5")
    assert old.config_actions == () and old.config[0].help_url_label == ""


@pytest.mark.parametrize(("extra", "reason"), [
    (SETTINGS + "[ConfigAction Setup]\nLabel=Go\n", "lowercase"),
    (SETTINGS + "[ConfigAction go]\nLabel=Go\n[ConfigAction go]\nLabel=Again\n", "duplicate"),
    (SETTINGS + "[ConfigAction go]\nIcon=go\n", "missing Label"),
    (SETTINGS + "[ConfigAction go]\nLabel=" + "x" * 41 + "\n", "Label"),
    (SETTINGS + "[ConfigAction go]\nLabel=Go‮\n", "control"),
    (SETTINGS + "[ConfigAction go]\nLabel=Go\nIcon=/usr/share/icons/x.png\n", "Icon"),
    (SETTINGS + "[ConfigAction go]\nLabel=Go\nPrimary=maybe\n", "Primary"),
    (SETTINGS + "[ConfigAction a]\nLabel=A\nPrimary=true\n[ConfigAction b]\nLabel=B\n"
     "Primary=true\n", "at most one"),
    (SETTINGS + "".join(f"[ConfigAction a{i}]\nLabel=A\n" for i in range(MAX_ACTIONS + 1)),
     "more than"),
    ("[ConfigAction go]\nLabel=Go\n", r"needs \[Config"),
    ("[Config port]\nLabel=Port\nType=int\nHelpUrlLabel=Why?\n", "needs a HelpUrl"),
    ("[Config port]\nLabel=Port\nType=int\nHelpUrl=https://example.org/\nHelpUrlLabel="
     + "x" * 41 + "\n", "HelpUrlLabel"),
])
def test_bad_actions_and_link_names_make_the_manifest_ignored(extra, reason) -> None:
    with pytest.raises(ManifestError, match=reason):
        _manifest(extra=extra)


def test_the_method_lives_on_plugin1() -> None:
    method = PluginService.RunConfigAction
    assert method._dbus_interface == PLUGIN_INTERFACE
    assert method.__name__ == METHOD_RUN_CONFIG_ACTION
    assert method._dbus_in_signature == "s" and method._dbus_out_signature == "s"


def test_fake_host_presses_a_button_and_checks_the_answer(tmp_path) -> None:
    service = inline_service(_Setup, _manifest())
    host = FakeHost(service, cache_root=tmp_path)
    result = host.config_action("setup_iphone")
    assert result == sf.ActionResult(True, "Scan the code", "http://127.0.0.1:4711/abc/")
    assert host.opened == ["http://127.0.0.1:4711/abc/"]
    # Text is one plain line; a file outside the plugin cache is dropped.
    other = host.config_action("new_token")
    assert other.ok and other.message == "New token [31m created" and other.open_uri is None
    assert host.opened == ["http://127.0.0.1:4711/abc/"]
    assert service.ran == ["setup_iphone", "new_token"]
    # An action may change the settings; the form reads them again.
    assert host.get_config() == {"port": 47802}


def test_undeclared_actions_never_reach_the_hook() -> None:
    service = inline_service(_Setup, _manifest())
    outcome: list[object] = []
    service.RunConfigAction("evil", reply=outcome.append, error=outcome.append, sender=":1.2")
    assert "no such settings action" in str(outcome[0]) and service.ran == []
    client = PluginClient(service.manifest, transport=ServiceTransport(service))
    with pytest.raises(PluginError, match="no such settings action"):
        client.run_config_action("evil")


def test_a_plugin_without_the_hook_fails_cleanly() -> None:
    service = inline_service(PluginService, _manifest())
    with pytest.raises(PluginError, match="no settings actions"):
        FakeHost(service).config_action("new_token")


def test_the_button_counts_against_the_caller_budget() -> None:
    service = inline_service(_Setup, _manifest())
    outcome: list[object] = []
    for _call in range(120):
        service.RunConfigAction("new_token", reply=outcome.append, error=outcome.append,
                                sender=":1.9")
    with pytest.raises(RateLimitedError):
        service.RunConfigAction("new_token", reply=outcome.append, error=outcome.append,
                                sender=":1.9")


@pytest.mark.parametrize("reply", ["not json", "[]", json.dumps({"ok": "yes"})])
def test_the_client_refuses_broken_answers(reply) -> None:
    client = PluginClient(_manifest(), transport=ScriptedTransport({METHOD_RUN_CONFIG_ACTION: reply}))
    with pytest.raises(PluginError):
        client.run_config_action("setup_iphone")


def test_the_client_refuses_a_plugin_of_another_user_before_the_click() -> None:
    transport = ScriptedTransport({METHOD_RUN_CONFIG_ACTION: json.dumps({"ok": True})}, uid=4242)
    client = PluginClient(_manifest(), transport=transport, uid=1000)
    with pytest.raises(PluginError, match="different user"):
        client.run_config_action("setup_iphone")
    assert transport.calls == []


def test_the_settings_button_example_in_plugins_md_parses() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "PLUGINS.md").read_text(encoding="utf-8")
    start = text.index("### Settings buttons")
    block = text[text.index("```\n", start) + 4:]
    block = block[:block.index("```")]
    parsed = _manifest(extra="[Config port]\nLabel=Port\nType=int\n" + block)
    (button,) = parsed.config_actions
    assert (button.id, button.label, button.icon, button.primary) == (
        "setup_iphone", "Set up iPhone", "smartphone", True)
