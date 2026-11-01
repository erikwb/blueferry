"""GTK drop zone of the main window on a private Broadway display.

The decisions are tested without a toolkit in ``tests/test_file_drop_view.py``;
these scenarios cover the overlay, the ``Gdk.FileList`` conversion, the
picker and the explanation. The shared model runs on a
:class:`tests.client_fakes.ManualRunner`, so nothing reaches a plugin.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from tests.gtk_broadway import run_scenario


@pytest.mark.parametrize("scenario", ["one", "several", "folder", "none", "pick", "remote",
                                      "long", "sending"])
def test_gtk_file_drop(tmp_path, scenario):
    run_scenario(tmp_path, str(Path(__file__).resolve()), scenario)


def _setup(choices, sending=None):
    from types import SimpleNamespace

    from blueferry import plugin_surfaces as surfaces
    from blueferry.file_drop_view import FileDrop
    from tests.client_fakes import ManualRunner
    from tests.test_gtk_phone_card import FakeSettings
    from tests.test_gtk_plugin_surfaces import _settle, _window

    work = ManualRunner()
    sent: list[tuple[str, list[str]]] = []

    def send(choice, files):
        sent.append((choice.key, [Path(f).name for f in files]))
        return surfaces.Outcome(True, "Sent.")

    def factory(**callbacks):
        return FileDrop(runner=work, load_targets=lambda: surfaces.ShareTargets(list(choices),
                                                                               []),
                        send=send, **callbacks)

    t = _window(file_drop=factory)
    if sending is not None:
        t.window.file_drop.model._is_sending = lambda: sending[0]
    toasts: list[str] = []
    longs: list[str] = []

    def toast(text: str, long: bool = False) -> None:
        toasts.append(text)
        if long:
            longs.append(text)

    t.window.file_drop._toast = toast
    tmp = Path(tempfile.mkdtemp())
    for name in ("a.txt", "b.txt"):
        (tmp / name).write_text(name)
    (tmp / "Pictures").mkdir()
    return SimpleNamespace(window=t.window, zone=t.window.file_drop, work=work, sent=sent,
                           toasts=toasts, longs=longs, tmp=tmp, settle=_settle,
                           settings=FakeSettings)


def _files(*paths):
    from gi.repository import Gdk, Gio

    return Gdk.FileList.new_from_list([
        Gio.File.new_for_uri(path) if "://" in str(path) else Gio.File.new_for_path(str(path))
        for path in paths
    ])


PHONE = ("io.example.ls", "LocalSend", "ls-1", "iPhone")
ABLAGE = ("io.example.ablage", "Ablage", "drop", "Ablage")


def _choices(*specs):
    from blueferry import plugin_surfaces as surfaces

    return [surfaces.ShareChoice(*spec) for spec in specs]


def _exercise_one():
    t = _setup(_choices(PHONE))
    zone = t.zone
    # The hint shows while files hover and never takes the drop itself.
    assert not zone.hint.get_visible() and not zone.hint.get_can_target()
    zone._enter(zone.target, 10, 10)
    assert zone.hint.get_visible()
    zone._leave(zone.target)
    assert not zone.hint.get_visible()
    assert zone._drop(zone.target, _files(t.tmp / "a.txt"), 10, 10) is True
    assert t.sent == []  # stat and plugins only on the worker
    t.work.run_tasks()
    t.settle()
    assert t.sent == [("io.example.ls:ls-1", ["a.txt"])]
    assert t.toasts == ["Sending a file to iPhone…", "Sent."]
    assert zone.dialog is None
    t.window.destroy()


def _exercise_several():
    t = _setup(_choices(PHONE))
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt", t.tmp / "b.txt"), 0, 0)
    t.work.run_tasks()
    assert t.sent == [("io.example.ls:ls-1", ["a.txt", "b.txt"])]
    assert t.toasts[0] == "Sending 2 files to iPhone…"
    t.window.destroy()


def _exercise_folder():
    t = _setup(_choices(PHONE))
    t.zone._drop(t.zone.target, _files(t.tmp / "Pictures"), 0, 0)
    t.work.run_tasks()
    assert t.sent == []
    assert t.toasts == ["Folders cannot be sent; skipped Pictures."]
    t.window.destroy()


def _exercise_none():
    t = _setup([])
    t.settings.opened = []
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt"), 0, 0)
    t.work.run_tasks()
    t.settle()
    dialog = t.zone.dialog
    assert dialog is not None
    assert "LocalSend" in dialog.get_body() and not dialog.get_body_use_markup()
    dialog.emit("response", "settings")
    t.settle()
    assert t.settings.opened == ["plugins"]
    assert t.zone.model.state()["phase"] == "idle"
    assert t.sent == []
    t.window.destroy()


def _exercise_pick():
    t = _setup(_choices(PHONE, ABLAGE))
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt"), 0, 0)
    t.work.run_tasks()
    t.settle()
    dialog = t.zone.dialog
    assert dialog is not None and t.sent == []
    buttons = t.zone.choice_buttons
    assert buttons["io.example.ls:ls-1"].get_active()
    buttons["io.example.ablage:drop"].set_active(True)
    dialog.emit("response", "send")
    t.work.run_tasks()
    assert t.sent == [("io.example.ablage:drop", ["a.txt"])]
    # The next drop preselects the last pick; Cancel sends nothing.
    t.zone._drop(t.zone.target, _files(t.tmp / "b.txt"), 0, 0)
    t.work.run_tasks()
    t.settle()
    assert t.zone.choice_buttons["io.example.ablage:drop"].get_active()
    t.zone.dialog.emit("response", "cancel")
    t.work.run_tasks()
    assert len(t.sent) == 1 and t.zone.dialog is None
    t.window.destroy()


def _exercise_remote():
    t = _setup(_choices(PHONE))
    assert t.zone._drop(t.zone.target, _files("https://example.org/a.jpg"), 0, 0) is True
    assert t.work.pending == []
    assert t.toasts == ["Skipped an item that is not on this computer."]
    assert t.zone._drop(t.zone.target, "not a file list", 0, 0) is False
    t.window.destroy()


def _exercise_long():
    # A failure stays longer than a progress notice, as in Qt.
    t = _setup(_choices(PHONE))
    t.zone._drop(t.zone.target, _files(t.tmp / "Pictures"), 0, 0)
    t.work.run_tasks()
    t.settle()
    assert t.longs == ["Folders cannot be sent; skipped Pictures."]
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt"), 0, 0)
    t.work.run_tasks()
    t.settle()
    assert t.toasts[-2:] == ["Sending a file to iPhone…", "Sent."]
    assert len(t.longs) == 1
    # The window's toast: longer and first in line for a failure.
    from blueferry.ui.window import Adw

    shown = []
    t.window._toasts.add_toast = shown.append
    t.window.toast("ok")
    t.window.toast("failed", True)
    assert shown[1].get_timeout() == 2 * shown[0].get_timeout()
    assert shown[1].get_priority() == Adw.ToastPriority.HIGH


def _exercise_sending():
    # A drop and a pick wait while the card's "Send to…" is sending.
    sending = [True]
    t = _setup(_choices(PHONE, ABLAGE), sending)
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt"), 0, 0)
    assert t.work.pending == []
    assert t.longs == ["Still busy with the last drop; try again in a moment."]
    sending[0] = False
    t.zone._drop(t.zone.target, _files(t.tmp / "a.txt"), 0, 0)
    t.work.run_tasks()
    t.settle()
    sending[0] = True
    t.zone.choice_buttons["io.example.ablage:drop"].set_active(True)
    t.zone.dialog.emit("response", "send")
    t.settle()
    assert t.sent == []
    # The picker comes back with the same choice.
    assert t.zone.dialog is not None
    assert t.zone.choice_buttons["io.example.ablage:drop"].get_active()
    sending[0] = False
    t.zone.dialog.emit("response", "send")
    t.work.run_tasks()
    t.settle()
    assert t.sent == [("io.example.ablage:drop", ["a.txt"])]


if __name__ == "__main__":
    {
        "one": _exercise_one,
        "several": _exercise_several,
        "folder": _exercise_folder,
        "none": _exercise_none,
        "pick": _exercise_pick,
        "remote": _exercise_remote,
        "long": _exercise_long,
        "sending": _exercise_sending,
    }[sys.argv[1]]()
