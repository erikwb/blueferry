"""ApiVersion 1.3 settings helpers: "Test connection" and browser sign-in.

``Plugin1.TestConfig(s values) -> s`` checks the values typed into the form
without storing them; a plugin offers it with ``ConfigTest=true``. The reply
is ``{"ok": bool, "message": str, "errors"?: {key: reason}}``.

``ConfigLogin=<provider>`` offers a browser sign-in, for example the
Nextcloud Login Flow v2:

1. ``ConfigLogin(s provider, s values) -> s``: ``values`` holds the form's
   non-secret values (typed, not yet saved; JSON object, at most 16 KiB),
   so the plugin knows the server. The plugin starts the flow and answers
   ``{"state": "open", "login_id": id, "open_uri": url, "message"?}``; the
   client opens ``open_uri`` (https, or http on localhost) in the browser.
   ``{"state": "done", "message"}`` means signed in without a browser,
   ``{"state": "error", "message"}`` that it could not start.
2. ``ConfigLoginStatus(s login_id) -> s``: the client polls every
   :data:`LOGIN_POLL_SECONDS` for at most :data:`LOGIN_TIMEOUT_SECONDS`.
   ``{"state": "pending"}`` until the user has signed in, then ``done``
   (the plugin has stored server, user and app password itself; ``message``
   says "Connected as …"), ``error``, ``expired`` or ``cancelled``.
3. ``ConfigLoginCancel(s login_id) -> s``: the user closed the form or
   pressed Cancel; the plugin stops polling its server. Answers
   ``{"ok": true}``.

Credentials never travel to the client: they go from the provider straight
into the plugin. Every message is one line of plain text.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

MAX_MESSAGE = 200
LOGIN_POLL_SECONDS = 2
# Nextcloud's Login Flow v2 tokens live 20 minutes.
LOGIN_TIMEOUT_SECONDS = 20 * 60
LOGIN_START_STATES = frozenset({"open", "done", "error"})
LOGIN_STATES = frozenset({"pending", "done", "error", "expired", "cancelled"})
LOGIN_FINAL_STATES = LOGIN_STATES - {"pending"}
_LOGIN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HTTPS = re.compile(r"^https://[^\s/?#]+(/\S*)?$")
_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?(/\S*)?$")
MAX_URI = 2048


class FlowError(ValueError):
    """A reply that breaks the contract."""


def plain(value: object, limit: int = MAX_MESSAGE) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def login_uri(value: object) -> str | None:
    """A sign-in URL a client may open: https, or http on this computer."""
    if not isinstance(value, str) or len(value) > MAX_URI:
        return None
    if _HTTPS.fullmatch(value) or _LOCAL.fullmatch(value):
        return value
    return None


@dataclass(frozen=True, slots=True)
class ConfigTestResult:
    ok: bool
    message: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"ok": self.ok, "message": plain(self.message)}
        if self.errors:
            result["errors"] = {
                plain(key, 32): plain(reason) for key, reason in self.errors.items()
            }
        return result


@dataclass(frozen=True, slots=True)
class LoginStep:
    """One answer of ConfigLogin or ConfigLoginStatus."""

    state: str
    message: str = ""
    login_id: str = ""
    open_uri: str = ""

    @property
    def final(self) -> bool:
        return self.state in LOGIN_FINAL_STATES

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"state": self.state}
        if self.message:
            result["message"] = plain(self.message)
        if self.login_id:
            result["login_id"] = self.login_id
        if self.open_uri:
            result["open_uri"] = self.open_uri
        return result


def _object(reply: object) -> Mapping[str, object]:
    try:
        value = json.loads(reply) if isinstance(reply, str) else reply
    except ValueError:
        raise FlowError("the plugin sent invalid JSON") from None
    if not isinstance(value, Mapping):
        raise FlowError("the plugin sent an invalid answer")
    return value


def _as_dict(value: object) -> Mapping[str, object]:
    if isinstance(value, (ConfigTestResult, LoginStep)):
        return value.to_dict()
    if isinstance(value, Mapping):
        return value
    raise TypeError("expected a result object or a dict")


def config_test_json(value: ConfigTestResult | Mapping[str, object]) -> str:
    """The plugin side: serialise and check a test_config() result."""
    return json.dumps(parse_config_test(_as_dict(value)).to_dict())


def parse_config_test(reply: object) -> ConfigTestResult:
    value = _object(reply)
    if not isinstance(value.get("ok"), bool):
        raise FlowError("the plugin sent an invalid test result")
    errors = value.get("errors")
    reasons = {
        plain(key, 32): plain(reason)
        for key, reason in (errors.items() if isinstance(errors, Mapping) else ())
    }
    message = plain(value.get("message", ""))
    if not message:
        message = "The settings work." if value["ok"] else "The test failed."
    return ConfigTestResult(bool(value["ok"]), message, reasons)


def login_step_json(value: LoginStep | Mapping[str, object], *, start: bool) -> str:
    """The plugin side: serialise and check a login hook's result."""
    return json.dumps(parse_login_step(_as_dict(value), start=start).to_dict())


def parse_login_step(reply: object, *, start: bool) -> LoginStep:
    """A ConfigLogin (``start=True``) or ConfigLoginStatus answer."""
    value = _object(reply)
    state = value.get("state")
    allowed = LOGIN_START_STATES if start else LOGIN_STATES
    if state not in allowed:
        raise FlowError("the plugin sent an invalid sign-in state")
    message = plain(value.get("message", ""))
    if state != "open":
        return LoginStep(str(state), message)
    login_id = value.get("login_id")
    if not isinstance(login_id, str) or not _LOGIN_ID.fullmatch(login_id):
        raise FlowError("the plugin sent an invalid sign-in id")
    uri = login_uri(value.get("open_uri"))
    if uri is None:
        raise FlowError("the plugin sent a sign-in address that is not https")
    return LoginStep("open", message, login_id, uri)


def valid_login_id(value: object) -> bool:
    return isinstance(value, str) and bool(_LOGIN_ID.fullmatch(value))


def form_values_json(values: Mapping[str, object]) -> str:
    """The non-secret form values a client hands to ConfigLogin."""
    return json.dumps({str(key): value for key, value in values.items()})
