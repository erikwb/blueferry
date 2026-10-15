"""Resuming iPhone playback after handing audio back must survive the
delay before iOS reports its pause over AMS."""
from __future__ import annotations

from types import SimpleNamespace

from blueferry import daemon as daemon_mod
from blueferry.ams.constants import RemoteCommandID


class _Media:
    def __init__(self, supported, playing):
        self.available = True
        self.state = SimpleNamespace(supported_commands=frozenset(supported), playing=playing)
        self.sent: list[str] = []

    def send_command(self, name, ok, failed):
        self.sent.append(name)


def _host(media):
    return SimpleNamespace(media=media, _resume_iphone_playback=None)


def _run(host, monkeypatch, timers):
    monkeypatch.setattr(daemon_mod.GLib, "timeout_add",
                        lambda ms, cb: timers.append(cb) or len(timers))
    bound = daemon_mod.Daemon._resume_iphone_playback.__get__(host)
    host._resume_iphone_playback = bound
    bound()


def test_explicit_play_is_sent_even_while_the_cached_state_still_says_playing(monkeypatch):
    media = _Media({RemoteCommandID.Play, RemoteCommandID.Pause}, playing=True)
    timers: list = []
    _run(_host(media), monkeypatch, timers)
    assert media.sent == ["play"] and timers == []


def test_toggle_only_players_wait_for_the_reported_pause(monkeypatch):
    media = _Media({RemoteCommandID.TogglePlayPause}, playing=True)
    host = _host(media)
    timers: list = []
    _run(host, monkeypatch, timers)
    assert media.sent == [] and len(timers) == 1
    media.state.playing = False
    timers.pop()()
    assert media.sent == ["play"]


def test_gives_up_after_the_retries_without_toggling_a_playing_player(monkeypatch):
    media = _Media({RemoteCommandID.TogglePlayPause}, playing=True)
    timers: list = []
    _run(_host(media), monkeypatch, timers)
    while timers:
        timers.pop()()
    assert media.sent == []
