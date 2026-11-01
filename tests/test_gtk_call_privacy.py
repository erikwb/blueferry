"""GTK call views discard private data and late work across access changes."""
from __future__ import annotations

import sys

import pytest

from tests.gtk_broadway import run_scenario

BOUNDARIES = ("lock", "profile", "daemon", "off", "hidden")


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("reply", ("success", "error"))
def test_call_history_access(tmp_path, boundary, reply):
    run_scenario(tmp_path, __file__, f"history:{boundary}:{reply}")


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_contact_search_access(tmp_path, boundary):
    run_scenario(tmp_path, __file__, f"contacts:{boundary}:success")


@pytest.mark.parametrize("boundary", ("profile", "daemon", "off", "hidden", "readiness"))
@pytest.mark.parametrize("reply", ("success", "error"))
def test_running_call_identity(tmp_path, boundary, reply):
    run_scenario(tmp_path, __file__, f"running:{boundary}:{reply}")


@pytest.mark.parametrize("kind", ("sync", "action"))
@pytest.mark.parametrize("reply", ("success", "error"))
def test_old_completion_cannot_finish_new_operation(tmp_path, kind, reply):
    run_scenario(tmp_path, __file__, f"{kind}:profile:{reply}")


@pytest.mark.parametrize("boundary", ("profile", "hidden"))
@pytest.mark.parametrize("reply", ("cached", "late"))
def test_call_avatars_follow_phone_and_window(tmp_path, boundary, reply):
    run_scenario(tmp_path, __file__, f"avatars:{boundary}:{reply}")


def _exercise(kind, boundary, reply):
    from blueferry.models import CallsSnapshot
    from tests.client_fakes import ManualRunner
    from tests.test_gtk_phone_card import (
        CALLS_READY,
        _descendants,
        _entries,
        _rows,
        _settle,
        _window,
    )

    status = {
        **CALLS_READY, "storage_state": "ready", "storage_policy": "encrypted",
        "call_history_enabled": True,
        "phone_profile_id": "11111111-1111-4111-8111-111111111111",
    }
    if kind == "avatars":
        status.update(contact_photos=True, contact_photo_revision=1)
    runner = ManualRunner()
    window, client = _window(status, timer=runner.timer)
    page, running = window.calls, window.calls.running
    window.show_tab("calls")
    _settle()
    history = ("calls", 200)
    call = {
        "call_id": "voicecall01", "state": "incoming", "number": "+15551230001",
        "contact_name": "Old caller", "first_seen": "2026-10-31T00:00:00Z",
    }
    snapshot = CallsSnapshot.from_dict({"state": "ready", "calls": [call]})
    toasts = []
    running._toast = toasts.append

    def change():
        changed = {
            "lock": {"storage_state": "locked"},
            "profile": {"phone_profile_id": "22222222-2222-4222-8222-222222222222"},
            "daemon": {"daemon": False},
            "off": {"call_history_enabled": False, "calls_enabled": False},
            "readiness": {"calls_state": "connecting"},
            "hidden": {},
        }[boundary]
        if boundary == "hidden":
            # Running calls follow the window, private lists follow the tab.
            if kind in ("running", "avatars"):
                window.set_visible(False)
            else:
                window.show_tab("notifications")
            _settle()
        else:
            client.answer_status({**status, **changed})
        return {**status, **changed} if boundary == "profile" else status

    def recover(current):
        if boundary == "hidden":
            window.present()
            window.show_tab("calls")
            _settle()
        client.answer_status(current)

    def complete(request, value):
        request[1](value) if reply == "success" else request[2]("stale failure")

    try:
        client.take(history)[1](_entries())
        client.take("list-calls")[1](snapshot)
        if kind == "history":
            assert _rows(page.recent_list)
            old_buttons = [w for w in _descendants(page.recent_list)
                           if isinstance(w, type(page.call_button))]
            actions = []
            page._dial = page._compose = actions.append
            client.emit("call-history-changed")
            old = client.take(history)
            current = change()
            assert page._calls == [] and _rows(page.recent_list) == []
            complete(old, _entries())
            assert page._calls == [] and page._calls_error == ""
            if boundary in ("lock", "daemon", "off", "hidden"):
                client.emit("call-history-changed")
                assert history not in client.names()
            recover(current)
            client.take(history)[1](_entries()[-1:])
            assert len(_rows(page.recent_list)) == 1
            assert "Anna" not in _rows(page.recent_list)[0].get_title()
            for button in old_buttons:
                button.emit("clicked")
            assert actions == []
        elif kind == "contacts":
            page.entry.set_text("Anna")
            page._search_timer.fire()
            client.take(("contacts", "Anna"))[1]([("Old contact", "+15551230001")])
            old_row = _rows(page.suggestions)[0]
            # A repeated query can be pending with existing suggestions visible.
            page._search_contacts()
            old = client.take(("contacts", "Anna"))
            assert _rows(page.suggestions)
            current = change()
            assert _rows(page.suggestions) == []
            old[1]([("Stale contact", "+15551230002")])
            assert _rows(page.suggestions) == []
            # An activation already queued for an old row must not fill the pad.
            page.suggestions.emit("row-activated", old_row)
            assert page.entry.get_text() == "Anna"
            recover(current)
            assert page._search_timer.active, "a still-typed query resumes on fresh access"
            page._search_timer.fire()
            client.take(("contacts", "Anna"))[1]([("Current contact", "+15551230003")])
            assert [row.get_title() for row in _rows(page.suggestions)] == ["Current contact"]
        elif kind == "running":
            old_buttons = running.rows[0][3]
            assert running._ticker.active
            client.emit("calls-changed")
            old = client.take("list-calls")
            current = change()
            assert running.rows == [] and not running.get_visible()
            assert not running._ticker.active
            complete(old, snapshot)
            assert running.calls == [] and running.rows == []
            connecting = client.take("list-calls") if boundary == "readiness" else None
            recover(current)
            if connecting is not None:
                complete(connecting, snapshot)
                assert running.rows == [], "the intervening readiness reply is stale too"
            client.take("list-calls")[1](CallsSnapshot.from_dict({
                "state": "ready", "calls": [{**call, "contact_name": "Current caller"}],
            }))
            for button in old_buttons:
                button.emit("clicked")
            assert not [n for n in client.names() if isinstance(n, tuple)
                        and n[0] in ("answer", "hangup")]
            running.rows[0][3][-1].emit("clicked")
            assert ("hangup", "voicecall01") in client.names()
        elif kind == "avatars":
            from blueferry.ui.avatars import decode_avatar
            from tests.test_gtk_avatars import _png

            photo = ("photo", call["number"])
            old = client.take(photo)
            texture = decode_avatar(_png())
            assert texture is not None
            if reply == "cached":
                old[1](texture)
                assert running.rows[0][2].get_custom_image() is texture
            current = change()
            if reply == "late" and boundary == "hidden":
                old[1](texture)
                assert photo not in client.names(), "hidden views must not refetch avatars"
            recover(current)
            client.take("list-calls")[1](snapshot)
            if reply == "late" and boundary == "profile":
                old[1](texture)
            assert running.rows[0][2].get_custom_image() is None
            fresh_texture = decode_avatar(_png(40, 40))
            client.take(photo)[1](fresh_texture)
            assert running.rows[0][2].get_custom_image() is fresh_texture
        else:
            if kind == "sync":
                page.sync()
                old = client.take("sync")
            else:
                running.rows[0][3][0].emit("clicked")
                old = client.take(("answer", "voicecall01"))
            current = change()
            recover(current)
            client.take(history)[1](_entries()[-1:])
            client.take("list-calls")[1](snapshot)
            if kind == "sync":
                page.sync()
                fresh = client.take("sync")
            else:
                running.rows[0][3][0].emit("clicked")
                fresh = client.take(("answer", "voicecall01"))
            before = client.names()
            complete(old, None)
            assert client.names() == before and toasts == []
            if kind == "sync":
                assert not page.sync_button.get_sensitive() and page._calls_error == ""
            else:
                assert all(not b.get_sensitive() for b in running.rows[0][3])
            fresh[1](None)
            assert (history if kind == "sync" else "list-calls") in client.names()
    finally:
        window.close()
        _settle()


if __name__ == "__main__":
    _exercise(*sys.argv[1].split(":"))
