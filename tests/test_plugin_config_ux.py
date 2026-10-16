"""Plugin API 1.3: the guided settings form, TestConfig and ConfigLogin."""
from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blueferry.plugin_api.client import PluginError
from blueferry.plugin_api.config import (
    SECRET_MASK,
    ConfigError,
    validate,
    value_text,
    visible_fields,
)
from blueferry.plugin_api.config_flow import (
    ConfigTestResult,
    FlowError,
    LoginStep,
    parse_config_test,
    parse_login_step,
)
from blueferry.plugin_api.manifest import ManifestError
from blueferry.plugin_api.service import CardService
from blueferry.plugin_api.testing import FakeHost, inline_service, manifest

FORM = """
[ConfigGroup account]
Label=Konto
Help=Where BlueFerry signs in.

[ConfigGroup images]
Label=Bilder

[Config url]
Label=Server URL
Type=url
Required=true
Group=account
Placeholder=https://cloud.example.com
Example=https://cloud.example.com/nextcloud
HelpUrl=https://docs.example.com/where-is-my-url
Help=The address you open in the browser.

[Config token]
Label=App password
Type=secret
Required=true
Group=account
Pattern=[A-Za-z0-9-]{5,72}
ErrorText=An app password has five to 72 letters, digits or dashes.

[Config upload_images]
Label=Upload pictures
Type=bool
Group=images

[Config image_size]
Label=Size
Type=choice
Choices=small;large;
Default=small
Group=images
ShowIf=upload_images=true

[Config image_quality]
Label=Quality
Type=int
Min=10
Max=100
Default=80
Required=true
ErrorText=Use a value from 10 to 100.
ShowIf=image_size=large
Group=images

[Config timeout]
Label=Timeout
Type=int
Min=1
Max=60
Default=15
Advanced=true
"""


def _form(extra: str = FORM, header: str = "", version: str = "1.3"):
    return manifest(capabilities="card;", api_version=version, extra=header + extra)


def test_the_manifest_reads_every_new_key() -> None:
    parsed = _form(header="ConfigTest=true\nConfigLogin=nextcloud\n")
    assert parsed.api_minor == 3 and parsed.config_test and parsed.config_login == "nextcloud"
    url, token, upload, size, quality, timeout = parsed.config
    assert url.placeholder == "https://cloud.example.com"
    assert url.example.endswith("/nextcloud")
    assert url.help_url == "https://docs.example.com/where-is-my-url"
    assert (url.group, token.group, upload.group, timeout.group) == (
        "account", "account", "images", "advanced",
    )
    assert token.pattern and token.error_text.startswith("An app password")
    assert size.show_if == ("upload_images", "true")
    assert quality.show_if == ("image_size", "large")
    groups = [(g.name, g.label, g.collapsed) for g in parsed.config_groups]
    assert groups == [
        ("account", "Konto", False), ("images", "Bilder", False),
        ("advanced", "Advanced", True),
    ]
    assert parsed.config_groups[0].help == "Where BlueFerry signs in."


def test_a_1_2_manifest_is_unchanged() -> None:
    old = manifest(api_version="1.2", capabilities="card;",
                   extra="[Config url]\nLabel=URL\nType=url\n")
    assert old.api_minor == 2 and not old.config_test and old.config_login == ""
    (field,) = old.config
    assert (field.placeholder, field.group, field.pattern, field.show_if) == ("", "", "", None)
    assert [g.name for g in old.config_groups] == [""]


def test_ungrouped_fields_come_first_and_advanced_last() -> None:
    parsed = _form(
        "[Config b]\nLabel=B\nType=string\nAdvanced=true\n"
        "[Config a]\nLabel=A\nType=string\n"
        "[Config c]\nLabel=C\nType=string\nGroup=options\n"
    )
    assert [g.name for g in parsed.config_groups] == ["", "options", "advanced"]
    assert parsed.config_groups[1].label == "Options"


@pytest.mark.parametrize("extra,reason", [
    ("[Config a]\nLabel=x\nType=string\nGroup=nope\n", "no \\[ConfigGroup\\]"),
    ("[Config a]\nLabel=x\nType=string\nGroup=account\nAdvanced=true\n", "conflicts"),
    ("[Config a]\nLabel=x\nType=string\nHelpUrl=http://example.org\n", "HelpUrl"),
    ("[Config a]\nLabel=x\nType=string\nHelpUrl=javascript:alert(1)\n", "HelpUrl"),
    ("[Config a]\nLabel=x\nType=string\nPlaceholder=" + "p" * 121 + "\n", "Placeholder"),
    ("[Config a]\nLabel=x\nType=string\nExample=a‮b\n", "Example"),
    ("[Config a]\nLabel=x\nType=string\nPattern=(a+)+\n", "simple subset"),
    ("[Config a]\nLabel=x\nType=string\nPattern=(?=a)\n", "simple subset"),
    ("[Config a]\nLabel=x\nType=string\nPattern=(a)\\1\n", "simple subset"),
    ("[Config a]\nLabel=x\nType=string\nPattern=[a\n", "not a regular"),
    ("[Config a]\nLabel=x\nType=bool\nPattern=a\n", "only for text"),
    ("[Config a]\nLabel=x\nType=string\nMin=3\n", "only for int"),
    ("[Config a]\nLabel=x\nType=string\nShowIf=b=1\n", "earlier setting"),
    ("[Config a]\nLabel=x\nType=string\nShowIf=a\n", "earlier setting"),
    ("[Config b]\nLabel=x\nType=bool\n[Config a]\nLabel=x\nType=string\nShowIf=b=yes\n",
     "true or false"),
    ("[Config b]\nLabel=x\nType=secret\n[Config a]\nLabel=x\nType=string\nShowIf=b=x\n",
     "secret"),
    ("[Config b]\nLabel=x\nType=choice\nChoices=p;q\n"
     "[Config a]\nLabel=x\nType=string\nShowIf=b=r\n", "not a choice"),
    ("[ConfigGroup Bad]\nLabel=x\n", "lowercase"),
    ("[ConfigGroup g]\nHelp=x\n", "missing Label"),
    ("[ConfigGroup g]\nLabel=" + "l" * 41 + "\n", "Label"),
    ("[ConfigGroup g]\nLabel=x\nCollapsed=maybe\n", "Collapsed"),
    ("[ConfigGroup g]\nLabel=x\n[ConfigGroup g]\nLabel=y\n", "duplicate"),
    ("".join(f"[ConfigGroup g{i}]\nLabel=x\n" for i in range(9)), "more than 8"),
])
def test_bad_new_keys_ignore_the_manifest(extra, reason) -> None:
    with pytest.raises(ManifestError, match=reason):
        _form(extra)


@pytest.mark.parametrize("header,reason", [
    ("ConfigTest=maybe\n", "ConfigTest"),
    ("ConfigLogin=Next Cloud\n", "ConfigLogin"),
])
def test_bad_plugin_keys_ignore_the_manifest(header, reason) -> None:
    with pytest.raises(ManifestError, match=reason):
        _form(header=header)
    with pytest.raises(ManifestError, match="need \\[Config"):
        _form("", header="ConfigTest=true\n")


def test_pattern_min_max_and_error_text_are_checked() -> None:
    fields = {field.key: field for field in _form().config}
    assert fields["token"].coerce("abcde-12") == "abcde-12"
    with pytest.raises(ConfigError, match="five to 72"):
        fields["token"].coerce("a b")
    with pytest.raises(ConfigError, match="from 10 to 100"):
        fields["image_quality"].coerce(5)
    # Without ErrorText the generic reasons stay.
    with pytest.raises(ConfigError, match="at most 60"):
        fields["timeout"].coerce(61)


def test_show_if_hides_fields_and_their_requirement() -> None:
    fields = _form().config
    keys = [field.key for field in visible_fields(fields, {})]
    assert "image_size" not in keys and "image_quality" not in keys
    on = {"upload_images": True, "image_size": "large"}
    assert {"image_size", "image_quality"} <= {f.key for f in visible_fields(fields, on)}
    # The chain breaks when the middle field is hidden, whatever its value.
    off = {"upload_images": False, "image_size": "large"}
    assert "image_quality" not in {f.key for f in visible_fields(fields, off)}
    current = {"url": "https://a.example", "token": True}
    _values, errors = validate(fields, current, {"image_quality": None})
    assert errors == {}
    _values, errors = validate(fields, current, {**on, "image_quality": None})
    assert errors == {"image_quality": "is required"}
    assert value_text(True) == "true" and value_text(None) == "" and value_text(3) == "3"


@given(st.text(max_size=60))
def test_a_pattern_never_raises_anything_but_config_error(text) -> None:
    field = _form().config[1]
    try:
        field.coerce(text)
    except ConfigError:
        pass


# ---- TestConfig and ConfigLogin over the full client validation ------------------

NEXTCLOUD = """
[Config url]
Label=Server URL
Type=url
Required=true

[Config user]
Label=User
Type=string

[Config app_password]
Label=App password
Type=secret
Required=true
"""


class _Nextcloud(CardService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stored: dict[str, object] = {}
        self.tested: list[dict] = []
        self.polls = 0
        self.cancelled: list[str] = []

    def config_values(self):
        return {**self.stored, "app_password": bool(self.stored.get("app_password"))}

    def apply_config(self, values) -> None:
        self.stored.update(values)

    def test_config(self, values):
        self.tested.append(dict(values))
        if values["url"] == "https://down.example":
            raise ConfigError("url", "the server did not answer")
        return ConfigTestResult(True, "Connected as anna\x1b[31m to Nextcloud 31")

    def config_login(self, provider, values):
        assert provider == "nextcloud" and "app_password" not in values
        if values["url"] == "https://flow.example":
            return LoginStep("open", login_id="flow-1",
                             open_uri="https://flow.example/login/v2/flow/abc")
        return {"state": "open", "login_id": "x", "open_uri": "http://evil.example/"}

    def config_login_status(self, login_id):
        self.polls += 1
        if self.polls < 3:
            return {"state": "pending"}
        self.stored.update(url="https://flow.example", user="anna", app_password="secret")
        return LoginStep("done", "Connected as anna")

    def config_login_cancel(self, login_id) -> None:
        self.cancelled.append(login_id)


def _host(header: str = "ConfigTest=true\nConfigLogin=nextcloud\n"):
    plugin = manifest("io.example.nextcloud", capabilities="card;", api_version="1.3",
                      extra=header + NEXTCLOUD)
    service = inline_service(_Nextcloud, plugin)
    return FakeHost(service), service


def test_test_config_checks_typed_values_without_saving() -> None:
    host, service = _host()
    result = host.test_config({"url": "https://cloud.example", "app_password": "typed"})
    assert result.ok and result.message.startswith("Connected as anna")
    assert "\x1b" not in result.message
    assert service.tested[-1]["app_password"] == "typed" and service.stored == {}
    result = host.test_config({"url": "https://down.example", "app_password": "x"})
    assert not result.ok and result.errors == {"url": "the server did not answer"}
    # The schema still runs first; nothing reaches the hook then.
    result = host.test_config({"url": "ftp://x"})
    assert not result.ok and set(result.errors) == {"url", "app_password"}
    assert len(service.tested) == 2


def test_sign_in_opens_the_browser_polls_and_never_returns_the_password() -> None:
    host, service = _host()
    step = host.sign_in({"url": "https://flow.example", "app_password": "ignored"})
    assert step.state == "done" and step.message == "Connected as anna"
    assert host.opened == ["https://flow.example/login/v2/flow/abc"]
    assert service.polls == 3
    shown = host.get_config()
    assert shown["user"] == "anna" and shown["app_password"] == SECRET_MASK
    assert "secret" not in json.dumps(shown)
    host.cancel_sign_in("flow-1")
    assert service.cancelled == ["flow-1"]


def test_sign_in_refuses_plain_http_bad_ids_and_missing_offers() -> None:
    host, _service = _host()
    with pytest.raises(PluginError, match="not https"):
        host.sign_in({"url": "https://other.example"})
    step = host.sign_in({"url": "ftp://nope"})
    assert step.state == "error" and step.message.startswith("url:")
    with pytest.raises(PluginError, match="sign-in id"):
        host.client.config_login_status("../x")
    plain, _service = _host(header="")
    with pytest.raises(PluginError, match="cannot test"):
        plain.test_config({})
    with pytest.raises(PluginError, match="no browser sign-in"):
        plain.sign_in({})
    # Called anyway (an old or rogue client), the plugin refuses too.
    outcome: list = []
    plain.service.TestConfig("{}", reply=outcome.append, error=outcome.append, sender=":1.2")
    plain.service.ConfigLogin("nextcloud", "{}", reply=outcome.append, error=outcome.append,
                              sender=":1.2")
    assert "cannot test" in str(outcome[0]) and "no browser sign-in" in str(outcome[1])


def test_an_unknown_provider_shows_no_button() -> None:
    host, _service = _host(header="ConfigLogin=future-cloud\n")
    assert host.client.login_provider() == ""


@pytest.mark.parametrize("reply", [
    "nope", "[]", '{"state": "maybe"}', '{"state": "open", "login_id": "a b", '
    '"open_uri": "https://x.example"}', '{"state": "open", "login_id": "a"}',
])
def test_broken_login_replies_are_errors(reply) -> None:
    with pytest.raises(FlowError):
        parse_login_step(reply, start=True)


def test_status_replies_are_plain_text_and_states_are_closed() -> None:
    step = parse_login_step('{"state": "expired", "message": "too\\nlate"}', start=False)
    assert step.final and step.message == "too late"
    with pytest.raises(FlowError):
        parse_login_step('{"state": "open"}', start=False)
    assert parse_config_test('{"ok": false}').message == "The test failed."
    with pytest.raises(FlowError):
        parse_config_test('{"ok": "yes"}')


# ---- the toolkit-free form helpers every client uses ---------------------------------


def test_the_example_manifest_in_plugins_md_parses() -> None:
    from pathlib import Path

    from blueferry.plugin_api.manifest import parse_manifest

    text = (Path(__file__).resolve().parents[1] / "PLUGINS.md").read_text(encoding="utf-8")
    start = text.index("Complete example")
    block = text[text.index("```\n", start) + 4:]
    block = block[:block.index("```")]
    parsed = parse_manifest(block)
    assert parsed.api_minor == 3 and parsed.config_test and parsed.config_login == "nextcloud"
    assert [g.name for g in parsed.config_groups] == ["account", "options", "pictures", "advanced"]
    keys = {field.key: field for field in parsed.config}
    assert keys["max_side"].show_if == ("resize", "true")
    assert keys["app_password"].coerce("abcde-fghij-klmno-pqrst-uvwxy")
