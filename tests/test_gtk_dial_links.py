"""GTK phone-link confirmation, without constructing widgets or opening a bus."""
from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import ClassVar

import pytest

pytest.importorskip("gi")

from gi.repository import GLib

from blueferry.models import BackendStatus
from blueferry.ui import app as app_module
from blueferry.ui import window as window_module
from blueferry.ui.dial_presenter import dial_prompt

READY = BackendStatus(daemon=True, calls_enabled=True, calls_state="ready")


@pytest.mark.parametrize(("problem", "status", "offer", "phrase"), [
    ("invalid", READY, "none", "does not contain a phone number"),
    ("emergency", READY, "none", "Emergency numbers"),
    ("", None, "settings", "not reachable"),
    ("", BackendStatus(daemon=True), "settings", "turned off"),
    ("", BackendStatus(daemon=True, calls_enabled=False), "settings", "turned off"),
    ("", BackendStatus(daemon=True, calls_enabled=True, calls_state="connecting"),
     "settings", "still connecting"),
    ("", BackendStatus(daemon=True, calls_enabled=True, calls_state="bluez_conflict"),
     "settings", "(bluez_conflict)"),
    ("", READY, "call", "Call +15550100000 from your iPhone?"),
])
def test_prompt_offers_a_call_only_when_calls_are_ready(problem, status, offer, phrase):
    prompt = dial_prompt("+15550100000" if problem != "invalid" else "", problem, "", status)
    assert prompt.offer == offer
    assert phrase in prompt.body


def test_prompt_names_a_known_contact_and_still_shows_the_number():
    prompt = dial_prompt("+15550100000", "", "Test Contact", READY)
    assert prompt.heading == "Place Call?"
    assert prompt.body == "Call Test Contact from your iPhone?\n+15550100000"


class _AlertDialog:
    shown: ClassVar[list[_AlertDialog]] = []

    def __init__(self, heading, body):
        self.heading, self.body = heading, body
        self.responses = []
        self.default = self.close = None
        self.handlers = []
        self.closed = False
        self.shown.append(self)

    def add_response(self, response_id, _label):
        self.responses.append(response_id)

    def set_default_response(self, response_id):
        self.default = response_id

    def set_close_response(self, response_id):
        self.close = response_id

    def set_response_appearance(self, *_args):
        pass

    def connect(self, _signal, handler):
        self.handlers.append(handler)

    def present(self, _parent):
        pass

    def force_close(self):
        self.closed = True
        self.respond(self.close)

    def respond(self, response_id):
        for handler in self.handlers:
            handler(self, response_id)


@pytest.fixture
def window(monkeypatch):
    _AlertDialog.shown = []
    monkeypatch.setattr(window_module, "Adw", SimpleNamespace(
        AlertDialog=_AlertDialog,
        ResponseAppearance=SimpleNamespace(SUGGESTED="suggested"),
    ))
    requests = {"status": [], "dial": [], "toasts": [], "settings": 0}

    def present_phone_settings():
        requests["settings"] += 1

    win = SimpleNamespace(
        _dial_request_serial=0,
        _dial_dialog=None,
        _client=SimpleNamespace(
            get_status_async=lambda ok, err: requests["status"].append((ok, err)),
            dial_async=lambda number, ok, err: requests["dial"].append((number, ok, err)),
        ),
        messages=SimpleNamespace(
            contact_name_for=lambda number: "Test Contact" if number == "+15550100000" else "",
        ),
        toast=requests["toasts"].append,
        present_phone_settings=present_phone_settings,
    )
    for name in ("request_dial", "_show_dial_prompt"):
        setattr(win, name, MethodType(getattr(window_module.MainWindow, name), win))
    return win, requests


def test_ready_calls_dial_only_after_the_call_response(window):
    win, requests = window
    win.request_dial("tel:+1-555-010-0000")
    assert requests["dial"] == [] and _AlertDialog.shown == []
    requests["status"].pop()[0](READY)
    dialog = _AlertDialog.shown[-1]
    assert dialog.responses == ["cancel", "call"]
    # Enter or Escape never places the call.
    assert dialog.default == dialog.close == "cancel"
    assert "Test Contact" in dialog.body and "+15550100000" in dialog.body
    dialog.respond("cancel")
    assert requests["dial"] == []
    # A cancelled dialog cannot be reused; open a new confirmation.
    dialog.respond("call")
    assert requests["dial"] == []
    win.request_dial("tel:+1-555-010-0000")
    requests["status"].pop()[0](READY)
    dialog = _AlertDialog.shown[-1]
    dialog.respond("call")
    number, ok, err = requests["dial"].pop()
    assert number == "+15550100000"
    ok("voicecall01")
    err("rate limited")
    assert requests["toasts"] == ["Calling +15550100000…", "rate limited"]


@pytest.mark.parametrize("reply", ["disabled", "failed"])
def test_disabled_or_unreachable_calls_offer_the_settings(window, reply):
    win, requests = window
    win.request_dial("callto:+15550100001")
    ok, err = requests["status"].pop()
    if reply == "disabled":
        ok(BackendStatus(daemon=True, calls_enabled=False))
    else:
        err("no backend")
    dialog = _AlertDialog.shown[-1]
    assert dialog.responses == ["cancel", "settings"]
    dialog.respond("settings")
    assert requests["settings"] == 1 and requests["dial"] == []


@pytest.mark.parametrize("stale_reply", ["success", "error"])
def test_a_late_status_reply_cannot_reopen_an_older_phone_link(window, stale_reply):
    win, requests = window
    win.request_dial("tel:+15550100000")
    old_ok, old_err = requests["status"].pop()
    win.request_dial("tel:+15550100001")
    requests["status"].pop()[0](READY)
    if stale_reply == "success":
        old_ok(READY)
    else:
        old_err("no backend")
    assert len(_AlertDialog.shown) == 1
    dialog = _AlertDialog.shown[-1]
    assert "+15550100001" in dialog.body
    dialog.respond("call")
    assert [request[0] for request in requests["dial"]] == ["+15550100001"]


def test_an_invalid_new_link_also_supersedes_pending_status(window):
    win, requests = window
    win.request_dial("tel:+15550100000")
    old_ok, _old_err = requests["status"].pop()
    win.request_dial("tel:7042;phone-context=example.com")
    old_ok(READY)
    assert len(_AlertDialog.shown) == 1
    assert _AlertDialog.shown[-1].responses == ["close"]
    assert requests["dial"] == []


def test_a_new_phone_link_closes_the_old_dialog_and_ignores_its_response(window):
    win, requests = window
    win.request_dial("tel:+15550100000")
    requests["status"].pop()[0](READY)
    old_dialog = _AlertDialog.shown[-1]
    win.request_dial("tel:+15550100001")
    assert old_dialog.closed
    old_dialog.respond("call")
    assert requests["dial"] == []
    requests["status"].pop()[0](READY)
    _AlertDialog.shown[-1].respond("call")
    assert [request[0] for request in requests["dial"]] == ["+15550100001"]


@pytest.mark.parametrize("link", [
    "tel:*21#", "tel:112", "https://example.com",
    "tel:7042;phone-context=example.com", "tel:+15550100000;ext=9",
    "tel:+15550100000;m-routing=private",
])
def test_invalid_and_emergency_links_only_explain(window, link):
    win, requests = window
    win.request_dial(link)
    # No status read is needed to refuse.
    assert requests["status"] == []
    dialog = _AlertDialog.shown[-1]
    assert dialog.responses == ["close"]
    dialog.respond("close")
    assert requests["dial"] == []


def _options(**values):
    options = GLib.VariantDict.new(None)
    for key, value in values.items():
        options.insert_value(key, GLib.Variant("s", value))
    return options


@pytest.mark.parametrize(("options", "arguments", "link"), [
    ({}, [], ""),
    ({}, ["tel:+15550100000"], "tel:+15550100000"),
    ({}, ["unrelated", "CALLTO:+15550100000"], "CALLTO:+15550100000"),
    ({"dial": "079 000 00 00"}, ["tel:+15550100000"], "079 000 00 00"),
    ({"message": "handle-1"}, [], ""),
    ({}, ["0790000000"], ""),
])
def test_command_line_takes_dial_option_or_a_bare_phone_link(options, arguments, link):
    assert app_module.dial_link(_options(**options), arguments) == link


def test_dial_activation_presents_the_window_then_asks():
    order = []
    win = SimpleNamespace(request_dial=lambda link: order.append(("dial", link)))

    def open_message(handle, token):
        order.append(("open", handle, token))
        return win

    application = SimpleNamespace(_open_message=open_message)
    app_module.BlueFerryApp._open_dial(application, "tel:+15550100000", "token")
    assert order == [("open", "", "token"), ("dial", "tel:+15550100000")]


@pytest.mark.parametrize(("forwarded", "arguments", "expected", "warned"), [
    (None, ["tel:+15550100000"], -1, False),  # no old window: normal startup
    (True, [], 0, False),
    (False, [], 1, False),
    # An old window cannot take the link; say so instead of losing it silently.
    (True, ["tel:+15550100000"], 1, True),
])
def test_phone_link_for_a_pre_upgrade_window_is_reported(
    monkeypatch, caplog, forwarded, arguments, expected, warned,
):
    monkeypatch.setattr(app_module, "forward_to_legacy_gtk", lambda _handle, _token: forwarded)
    application = SimpleNamespace(_local_arguments=arguments)
    with caplog.at_level("WARNING"):
        result = app_module.BlueFerryApp.do_handle_local_options(application, _options())
    assert result == expected
    assert ("older version" in caplog.text) is warned


def test_run_remembers_the_positional_arguments(monkeypatch):
    monkeypatch.setattr(app_module.Adw.Application, "run", lambda _self, _argv: 0)
    application = SimpleNamespace(_local_arguments=[])
    assert app_module.BlueFerryApp.run(application, ["blueferry-gtk", "tel:+1"]) == 0
    assert application._local_arguments == ["tel:+1"]
