"""Plugin clients and settings shared by the plugin settings tests (model and Qt)."""
from __future__ import annotations

import json
from typing import ClassVar

from blueferry.plugin_api.client import ConfigResult, PluginStatus
from blueferry.plugin_api.config import SECRET_MASK
from tests.test_plugin_manager import URL


class DemoClient:
    saved: ClassVar[list[dict]] = []

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def status(self):
        return PluginStatus("unconfigured", "set a server")

    def get_config(self):
        return {"url": "https://a.example", "api_key": SECRET_MASK, "camera_model": ""}

    def set_config(self, values):
        DemoClient.saved.append(dict(values))
        if values.get("url") == "https://bad.example":
            return ConfigResult(False, {"url": "not reachable"})
        return ConfigResult(True, {})


INDEX = json.dumps({"version": 1, "plugins": [
    {"id": "io.example.demo", "name": "Demo photos", "description": "Photos.",
     "repo": URL, "ref": "v0.1.0", "capabilities": ["photos"], "icon": "folder-pictures"},
    {"id": "io.example.cal", "name": "Calendar", "repo": URL + "-cal", "ref": None,
     "capabilities": ["calendar"]},
]}).encode()


class GuidedClient:
    """A 1.3 plugin: test connection and a Nextcloud sign-in."""

    log: ClassVar[list[tuple]] = []
    polls: ClassVar[int] = 0

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def get_config(self):
        signed_in = GuidedClient.polls >= 2
        return {"url": "https://cloud.example" if signed_in else "",
                "app_password": SECRET_MASK if signed_in else ""}

    def set_config(self, values):
        GuidedClient.log.append(("save", dict(values)))
        if values.get("url") == "https://bad.example":
            return ConfigResult(False, {"url": "not reachable"})
        return ConfigResult(True, {})

    def test_config(self, values):
        from blueferry.plugin_api.config_flow import ConfigTestResult

        GuidedClient.log.append(("test", dict(values)))
        if values.get("url") == "https://down.example":
            return ConfigTestResult(False, "The server did not answer.", {"url": "unreachable"})
        return ConfigTestResult(True, "Connected as anna.")

    def config_login(self, values):
        from blueferry.plugin_api.config_flow import LoginStep

        GuidedClient.log.append(("login", dict(values)))
        return LoginStep("open", login_id="flow-1", open_uri="https://cloud.example/login/v2/f")

    def config_login_status(self, login_id):
        from blueferry.plugin_api.config_flow import LoginStep

        GuidedClient.polls += 1
        if GuidedClient.polls < 2:
            return LoginStep("pending")
        return LoginStep("done", "Connected as anna")

    def config_login_cancel(self, login_id):
        GuidedClient.log.append(("cancel", login_id))

    def status(self):
        return PluginStatus("ok")


GUIDED = """
[Config url]
Label=Server URL
Type=url
Required=true
Placeholder=https://cloud.example.com
HelpUrl=https://docs.example.com/url
Group=account

[Config app_password]
Label=App password
Type=secret
Required=true
Group=account

[Config timeout]
Label=Timeout
Type=int
Min=1
Max=60
Default=15
Advanced=true
"""


class NoManager:
    def entries(self):
        return [], []

    def settings(self):
        return {}
