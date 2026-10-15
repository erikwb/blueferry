"""Delayed GTK overview reads, without constructing widgets or opening a bus."""
from types import MethodType, SimpleNamespace

from blueferry.models import BackendStatus
from blueferry.ui.phone import PhonePage


def _page():
    requests = {kind: [] for kind in ("status", "media", "calls")}
    page = SimpleNamespace(
        _watching=False, _epoch=0, _pending=set(), _again=set(),
        _status=BackendStatus().to_dict(), _now_playing={}, _calls=[],
        _calls_error="", _calls_received=False,
        _render=lambda: None, _render_media=lambda: None, _render_calls=lambda: None,
        _client=SimpleNamespace(
            get_status_async=lambda ok, err: requests["status"].append((ok, err)),
            now_playing_async=lambda ok, err: requests["media"].append((ok, err)),
            call_history_async=lambda limit, ok, err: requests["calls"].append((ok, err)),
        ),
    )
    for name in ("set_watching", "_load", "_refresh_status", "_refresh_media", "_refresh_calls",
                 "_status_loaded", "_media_loaded", "_calls_loaded", "_calls_failed"):
        setattr(page, name, MethodType(getattr(PhonePage, name), page))
    page.set_watching(True)
    requests["status"].pop()[0](BackendStatus.from_dict({
        "daemon": True, "media_control_enabled": True, "call_history_enabled": True,
    }))
    return page, requests


def test_gtk_overview_bounds_signal_bursts_and_fetches_the_latest_snapshot():
    page, requests = _page()
    for _ in range(1000):
        page._refresh_media()
        page._refresh_calls()
        page._refresh_status()
    assert {key: len(value) for key, value in requests.items()} == {
        "media": 1, "calls": 1, "status": 1,
    }
    requests["media"][0][0]({"available": True})
    assert len(requests["media"]) == 2


def test_gtk_overview_discards_media_and_calls_after_opt_out():
    page, requests = _page()
    page._status_loaded(BackendStatus.from_dict({"daemon": True}))
    requests["media"][0][0]({"available": True, "track": {"title": "old"}})
    requests["calls"][0][0](["old call"])
    assert page._now_playing == {}
    assert page._calls == []
    assert not page._pending


def test_gtk_overview_discards_closed_dialog_replies_and_reloads_on_reopen():
    page, requests = _page()
    page.set_watching(False)
    requests["media"][0][0]({"available": True})
    requests["calls"][0][1]("old failure")
    assert page._now_playing == {} and page._calls_error == ""
    page.set_watching(True)
    assert len(requests["status"]) == 1
    requests["status"][0][0](BackendStatus.from_dict({
        "daemon": True, "media_control_enabled": True,
    }))
    assert len(requests["media"]) == 2
