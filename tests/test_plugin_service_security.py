"""Plugin hooks are private to the service's Unix user, even on shared buses."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blueferry.plugin_api.service import (
    AccessDeniedError,
    CardService,
    NotifyService,
    PhotosService,
    ShareService,
)
from blueferry.plugin_api.surfaces import SendResult
from blueferry.plugin_api.testing import inline_service, manifest


class Credentials:
    def __init__(self, uid: object) -> None:
        self.uid = uid
        self.senders: list[str] = []

    def call_blocking(self, destination, path, interface, method, signature, args, *, timeout):
        assert (destination, path, interface, method, signature) == (
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionUnixUser", "s",
        )
        assert 0 < timeout <= 1
        self.senders.append(args[0])
        if isinstance(self.uid, Exception):
            raise self.uid
        return self.uid


class Plugin(PhotosService, CardService, ShareService, NotifyService):
    def send_files(self, target_id, paths):
        return SendResult(True, Path(paths[0]).read_text())


def plugin(**kwargs):
    return inline_service(
        Plugin, manifest(capabilities="photos;card;share;notify;"), **kwargs,
    )


@pytest.mark.parametrize(("method", "args"), [
    ("GetInfo", ()), ("Status", ()), ("GetConfig", ()), ("SetConfig", ("{}",)),
    ("TestConfig", ("{}",)), ("ConfigLogin", ("nextcloud", "{}")),
    ("ConfigLoginStatus", ("login",)), ("ConfigLoginCancel", ("login",)),
    ("RunConfigAction", ("setup_iphone",)),
    ("ListRecent", (1,)), ("FetchOriginal", ("photo",)), ("GetCardItems", ()),
    ("InvokeAction", ("card", "action", "{}")), ("ShareTargets", ()),
    ("SendFiles", ("target", ["/must-not-be-read"])), ("GetPendingNotification", ()),
])
def test_foreign_user_cannot_call_any_plugin_method(method, args) -> None:
    now = [10.0]
    workers: list = []
    service = plugin(clock=lambda: now[0], start_worker=workers.append)
    service._connection = Credentials(os.getuid() + 1)
    service._host_owner = ":1.7"  # Owning the daemon name does not bypass the UID check.
    service.NotifyChanged = lambda: None
    service.emit_notify("Private", "Still queued")
    now[0] = 20.0
    call = getattr(service, method)
    callbacks = {"reply": lambda result: None, "error": lambda error: None} if getattr(
        call, "_dbus_async_callbacks", None,
    ) else {}
    with pytest.raises(AccessDeniedError) as denied:
        call(*args, **callbacks, sender=":1.7")
    assert denied.value.get_dbus_name() == "org.freedesktop.DBus.Error.AccessDenied"
    assert not workers and service.in_flight == 0
    assert service.last_activity == 10.0  # Unauthorized calls cannot postpone idle exit.
    assert len(service._pending) == 1


@pytest.mark.parametrize("sender", [None, "", "io.example.app"])
def test_exported_service_requires_a_unique_sender(sender) -> None:
    service = plugin()
    credentials = Credentials(os.getuid())
    service._connection = credentials
    with pytest.raises(AccessDeniedError):
        service.GetInfo(sender=sender)
    assert not credentials.senders


@pytest.mark.parametrize("uid", [None, "invalid", RuntimeError("bus unavailable")])
def test_unverifiable_credentials_fail_closed(uid) -> None:
    service = plugin()
    service._connection = Credentials(uid)
    with pytest.raises(AccessDeniedError, match="could not be verified"):
        service.Status(sender=":1.7")


def test_share_hook_runs_only_for_verified_same_user(tmp_path) -> None:
    canary = tmp_path / "private.txt"
    canary.write_text("synthetic audit canary")
    canary.chmod(0o600)
    service = plugin()
    credentials = Credentials(os.getuid())
    service._connection = credentials
    replies, errors = [], []
    service.SendFiles("target", [str(canary)], replies.append, errors.append, sender=":1.7")
    assert not errors and json.loads(replies[0])["message"] == canary.read_text()
    assert credentials.senders == [":1.7"]
    # A new export/bus must not reuse any earlier credential evidence.
    service._connection = Credentials(os.getuid() + 1)
    with pytest.raises(AccessDeniedError):
        service.SendFiles("target", [str(canary)], replies.append, errors.append, sender=":1.7")
    assert len(replies) == 1


def test_unexported_service_still_supports_the_in_process_harness() -> None:
    service = plugin()
    assert json.loads(service.Status())["state"] == "ok"
