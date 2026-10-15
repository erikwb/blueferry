"""Plugin API 1.4: card actions that send files and ReplacesTools."""
from __future__ import annotations

import pytest

from blueferry.plugin_api import KNOWN_TOOLS, TOOL_LOCALSEND
from blueferry.plugin_api import surfaces as sf
from blueferry.plugin_api.client import PluginError
from blueferry.plugin_api.manifest import ManifestError
from blueferry.plugin_api.service import CardService, ShareService
from blueferry.plugin_api.testing import FakeHost, inline_service, manifest


class _Sender(CardService, ShareService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[tuple[str, list[str]]] = []
        self.invoked: list[tuple[str, str]] = []

    def card_items(self):
        return [sf.CardItem("dev-phone", "iPhone", icon="phone", actions=[
            sf.Action("send", "Send files…", "document-send", "primary", send_to="phone"),
            sf.Action("trust", "Trust"),
        ])]

    def invoke_action(self, item_id, action_id, args):
        self.invoked.append((item_id, action_id))
        return sf.ActionResult(False, "Use Send to…")

    def share_targets(self):
        return [sf.ShareTarget("phone", "iPhone", "phone")]

    def send_files(self, target_id, paths):
        self.sent.append((target_id, paths))
        return sf.SendResult(True, "Sending", "job-1")


def _manifest(caps: str = "card;share;", extra: str = ""):
    return manifest("io.example.sender", capabilities=caps, api_version="1.4", extra=extra)


def test_send_to_is_serialised_only_when_set_and_parsed_back() -> None:
    plain = sf.Action("open", "Open").to_dict()
    assert "send_to" not in plain  # a 1.3 host checking exact keys still accepts it
    sending = sf.Action("send", "Send", kind="primary", send_to="ls-1")
    assert sending.to_dict()["send_to"] == "ls-1"
    item = sf.parse_card_items({"items": [{
        "id": "d", "title": "Phone",
        "actions": [plain, sending.to_dict(), {"id": "x", "label": "Bad", "send_to": "a:b"}],
    }]})[0]
    assert [a.send_to for a in item.actions] == [None, "ls-1", None]
    assert item.send_target == "ls-1"
    assert sf.CardItem("e", "Empty").send_target is None
    with pytest.raises(ValueError):
        sf.Action("send", "Send", send_to="../etc")


def test_fake_host_sends_through_the_action_without_invoking(tmp_path) -> None:
    service = inline_service(_Sender, _manifest())
    host = FakeHost(service)
    picked = tmp_path / "a.txt"
    picked.write_text("x")
    result = host.send_action("dev-phone", "send", [str(picked)])
    assert result.ok and result.job == "job-1"
    assert service.sent == [("phone", [str(picked)])]
    assert service.invoked == []
    with pytest.raises(PluginError, match="does not send"):
        host.send_action("dev-phone", "trust", [str(picked)])
    with pytest.raises(PluginError, match="no card item"):
        host.send_action("gone", "send", [str(picked)])


def test_a_sending_action_needs_the_share_capability(tmp_path) -> None:
    host = FakeHost(inline_service(_Sender, _manifest("card;")))
    with pytest.raises(PluginError, match="share capability"):
        host.send_action("dev-phone", "send", [str(tmp_path)])


def test_replaces_tools_keeps_known_names_and_rejects_junk() -> None:
    assert TOOL_LOCALSEND in KNOWN_TOOLS
    parsed = _manifest(extra="ReplacesTools=localsend; future-tool;localsend;\n")
    assert parsed.replaces_tools == ("localsend",)
    assert _manifest().replaces_tools == ()
    with pytest.raises(ManifestError, match="ReplacesTools"):
        _manifest(extra="ReplacesTools=Local Send\n")
