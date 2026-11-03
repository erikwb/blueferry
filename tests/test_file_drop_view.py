"""Files dropped on the main window: decisions and strings shared by Qt and GTK.

No toolkit, no plugin process: fake targets and a manual runner.
"""
from __future__ import annotations

from pathlib import Path

from blueferry import file_drop_view as drop
from blueferry import plugin_surfaces as surfaces
from blueferry.plugin_api.surfaces import MAX_SHARE_FILES
from blueferry.plugin_surfaces_model import PluginSurfacesModel, SendGate
from tests.client_fakes import ManualRunner

PHONE = surfaces.ShareChoice("io.example.localsend", "LocalSend", "ls-1", "iPhone")
ABLAGE = surfaces.ShareChoice("io.example.ablage", "Ablage", "drop", "Ablage", "folder-cloud")


def _files(tmp_path: Path, *names: str) -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text("x", encoding="utf-8")
        paths.append(path)
    return paths


class Harness:
    def __init__(self, choices=(), problems=()) -> None:
        self.work = ManualRunner()
        self.gate = SendGate()
        self.notices: list[tuple[str, bool]] = []
        self.sent: list[tuple[str, list[str]]] = []
        self.loads = 0
        self.after_send = 0
        self.targets = surfaces.ShareTargets(list(choices), list(problems))
        self.ok = True

        def load() -> surfaces.ShareTargets:
            self.loads += 1
            return self.targets

        def send(choice, paths):
            self.sent.append((choice.key, list(paths)))
            return surfaces.Outcome(self.ok, "Sent." if self.ok else "Refused.")

        self.model = drop.FileDrop(
            runner=self.work, notify=lambda text, ok: self.notices.append((text, ok)),
            on_sent=self._sent, load_targets=load, send=send,
            gate=self.gate,
        )

    def _sent(self) -> None:
        self.after_send += 1

    def run_all(self) -> None:
        while self.work.pending:
            self.work.run_next()


def test_only_local_file_uris_become_paths() -> None:
    result = drop.local_paths([
        "file:///home/me/a%20b.txt", "file://localhost/home/me/c.txt",
        "https://example.com/x", "sftp://host/file", "file://otherhost/etc/passwd",
        "file:relative", "", "file:///home/me/a%20b.txt",
    ])
    assert result.paths == ("/home/me/a b.txt", "/home/me/c.txt")
    assert result.remote == 4


def test_a_malformed_uri_is_skipped_without_losing_the_drop() -> None:
    result = drop.local_paths(["file:///home/me/a.txt", "https://[bad"])
    assert result.paths == ("/home/me/a.txt",)
    assert result.remote == 1


def test_a_name_that_is_not_utf8_is_skipped_not_replaced(tmp_path) -> None:
    # Replacing the bad byte would name this other, existing file.
    lookalike = tmp_path / "photo-\ufffd.jpg"
    lookalike.write_text("x", encoding="utf-8")
    harness = Harness([PHONE])
    harness.model.drop([tmp_path.as_uri() + "/photo-%FF.jpg"])
    assert harness.work.pending == []
    assert harness.sent == []
    assert harness.notices == [("Skipped an item whose name is not UTF-8 text.", False)]


def test_a_file_gone_at_drop_time_is_named_as_gone(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])
    harness.model.drop([one.as_uri(), (tmp_path / "gone.txt").as_uri()])
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])]
    assert ("Skipped an item that no longer exists.", False) in harness.notices


def test_one_file_with_one_target_is_sent_with_a_notice(tmp_path) -> None:
    (one,) = _files(tmp_path, "photo.jpg")
    harness = Harness([PHONE])
    harness.model.drop([one.as_uri()])
    assert harness.model.state()["phase"] == drop.CHECKING
    assert harness.loads == 0  # nothing blocking before the runner runs
    harness.work.run_next()
    assert harness.model.state()["phase"] == drop.SENDING
    assert harness.notices == [("Sending a file to iPhone…", True)]
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])]
    assert harness.notices[-1] == ("Sent.", True)
    assert harness.after_send == 1
    assert harness.model.state()["phase"] == drop.IDLE


def test_several_files_go_out_in_one_send(tmp_path) -> None:
    files = _files(tmp_path, "a.txt", "b.txt", "c.txt")
    harness = Harness([PHONE])
    harness.model.drop([path.as_uri() for path in files])
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(path.resolve()) for path in files])]
    assert harness.notices[0] == ("Sending 3 files to iPhone…", True)


def test_a_folder_is_skipped_and_said_so(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    folder = tmp_path / "Pictures"
    folder.mkdir()
    harness = Harness([PHONE])
    harness.model.drop([one.as_uri(), folder.as_uri()])
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])]
    assert ("Folders cannot be sent; skipped Pictures.", False) in harness.notices


def test_only_a_folder_sends_nothing_and_starts_no_plugin(tmp_path) -> None:
    harness = Harness([PHONE])
    harness.model.drop([tmp_path.as_uri()])
    harness.run_all()
    assert harness.sent == []
    assert harness.loads == 0
    assert harness.notices == [(f"Folders cannot be sent; skipped {tmp_path.name}.", False)]
    assert harness.model.state()["phase"] == drop.IDLE


def test_remote_uris_alone_are_refused_without_the_runner() -> None:
    harness = Harness([PHONE])
    harness.model.drop(["https://example.com/a.jpg"])
    assert harness.work.pending == []
    assert harness.notices == [("Skipped an item that is not on this computer.", False)]


def test_no_target_explains_which_plugins_provide_one(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([], ["Ablage: timeout"])
    harness.model.drop([one.as_uri()])
    harness.run_all()
    state = harness.model.state()
    assert state["phase"] == drop.NO_TARGET
    assert "LocalSend" in state["body"] and "Settings > Plugins" in state["body"]
    assert "Ablage: timeout" in state["body"]
    assert harness.sent == []
    harness.model.cancel()
    assert harness.model.state()["phase"] == drop.IDLE


def test_several_targets_ask_and_remember_the_choice(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE, ABLAGE])
    harness.model.drop([one.as_uri()])
    harness.run_all()
    state = harness.model.state()
    assert state["phase"] == drop.PICKING
    assert [row["key"] for row in state["choices"]] == [PHONE.key, ABLAGE.key]
    assert state["selected"] == PHONE.key
    assert state["body"] == "Where should the file go?"
    harness.model.pick(ABLAGE.key)
    harness.run_all()
    assert harness.sent == [(ABLAGE.key, [str(one.resolve())])]

    harness.model.drop([one.as_uri()])
    harness.run_all()
    assert harness.model.state()["selected"] == ABLAGE.key
    harness.model.cancel()
    assert harness.model.state()["phase"] == drop.IDLE
    assert len(harness.sent) == 1


def test_an_unknown_pick_sends_nothing(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE, ABLAGE])
    harness.model.drop([one.as_uri()])
    harness.run_all()
    harness.model.pick("io.example.gone:x")
    assert harness.work.pending == []
    assert harness.model.state()["phase"] == drop.PICKING


def test_a_second_drop_waits_for_the_first(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])
    harness.model.drop([one.as_uri()])
    harness.model.drop([one.as_uri()])
    assert harness.work.submitted == 1
    assert harness.notices[-1][1] is False


def test_more_files_than_the_api_allows_are_refused(tmp_path) -> None:
    files = _files(tmp_path, *(f"f{index}.txt" for index in range(MAX_SHARE_FILES + 1)))
    harness = Harness([PHONE])
    harness.model.drop([path.as_uri() for path in files])
    harness.run_all()
    assert harness.sent == []
    assert str(MAX_SHARE_FILES) in harness.notices[-1][0]


def test_the_file_limit_keeps_the_skipped_note(tmp_path) -> None:
    files = _files(tmp_path, *(f"f{index}.txt" for index in range(MAX_SHARE_FILES + 1)))
    folder = tmp_path / "Album"
    folder.mkdir()
    harness = Harness([PHONE])
    harness.model.drop([path.as_uri() for path in files] + [folder.as_uri()])
    harness.run_all()
    text, ok = harness.notices[-1]
    assert not ok and str(MAX_SHARE_FILES) in text
    assert "Folders cannot be sent; skipped Album." in text


def test_a_file_deleted_before_sending_is_reported(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE, ABLAGE])
    harness.model.drop([one.as_uri()])
    harness.run_all()
    one.unlink()
    harness.model.pick(PHONE.key)
    harness.run_all()
    assert harness.sent == []
    # Said in this module's (translatable) words, not the plugin API's.
    assert harness.notices[-1] == (
        "Nothing was sent: the dropped files changed in the meantime. "
        "Skipped an item that no longer exists.", False)
    assert harness.after_send == 0


def test_a_file_swapped_for_a_symlink_is_not_sent(tmp_path) -> None:
    (one, secret) = _files(tmp_path, "a.txt", "secret.txt")
    harness = Harness([PHONE, ABLAGE])
    harness.model.drop([one.as_uri()])
    harness.run_all()
    one.unlink()
    one.symlink_to(secret)  # same count, still one regular file
    harness.model.pick(PHONE.key)
    harness.run_all()
    assert harness.sent == []
    assert harness.notices[-1] == (
        "Nothing was sent: the dropped files changed in the meantime.", False)


def test_a_file_turned_into_a_folder_is_named(tmp_path) -> None:
    (one, two) = _files(tmp_path, "a.txt", "Album")
    harness = Harness([PHONE, ABLAGE])
    harness.model.drop([one.as_uri(), two.as_uri()])
    harness.run_all()
    two.unlink()
    two.mkdir()
    harness.model.pick(PHONE.key)
    harness.run_all()
    assert harness.sent == []
    assert harness.notices[-1] == (
        "Nothing was sent: the dropped files changed in the meantime. "
        "Folders cannot be sent; skipped Album.", False)


def _send_to(harness: Harness, choice: surfaces.ShareChoice) -> PluginSurfacesModel:
    """The client's "Send to…", sharing the drop's gate and send log."""
    send_to = PluginSurfacesModel(
        runner=harness.work, timer=harness.work.timer, opener=lambda _uri: None,
        load_targets=lambda: surfaces.ShareTargets([choice], []),
        send=lambda target, files: harness.sent.append((target.key, list(files)))
        or surfaces.Outcome(True, "Sent."),
        gate=harness.gate,
    )
    send_to.load_targets()
    harness.work.run_next()
    return send_to


def test_a_drop_waits_while_send_to_is_sending(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE, ABLAGE])
    send_to = _send_to(harness, PHONE)
    send_to.send(PHONE.key, [str(one)])
    harness.model.drop([one.as_uri()])
    assert harness.work.submitted == 2  # targets and the send; no drop work
    assert harness.notices == [
        ("Other files are still being sent; try again in a moment.", False)]
    harness.run_all()
    # Started meanwhile: the picker waits instead of sending alongside.
    harness.model.drop([one.as_uri()])
    harness.run_all()
    send_to.send(PHONE.key, [str(one)])
    harness.model.pick(PHONE.key)
    assert len(harness.work.pending) == 1
    assert harness.model.state()["phase"] == drop.PICKING
    harness.run_all()
    harness.model.pick(PHONE.key)
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])] * 3


def test_send_to_started_while_a_single_target_drop_is_checked(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])
    send_to = _send_to(harness, PHONE)
    harness.model.drop([one.as_uri()])
    send_to.send(PHONE.key, [str(one)])  # the drop is still being checked
    harness.work.run_next()  # the check ends: the drop must not send alongside
    assert len(harness.work.pending) == 1
    assert harness.model.state()["phase"] == drop.IDLE
    assert harness.notices[-1] == (
        "Other files are still being sent; try again in a moment.", False)
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])]


def test_send_to_waits_while_a_drop_is_sending(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])
    send_to = _send_to(harness, PHONE)
    harness.model.drop([one.as_uri()])
    harness.work.run_next()
    assert harness.model.state()["phase"] == drop.SENDING
    send_to.send(PHONE.key, [str(one)])
    send_to.send_from_card(PHONE.plugin_id, PHONE.target_id, "iPhone", [str(one)])
    assert send_to.state()["busy"] == ""
    assert send_to.state()["message"] == (
        "Other files are still being sent; try again in a moment.")
    harness.run_all()
    assert harness.sent == [(PHONE.key, [str(one.resolve())])]
    send_to.send(PHONE.key, [str(one)])  # the drop is done: free again
    harness.run_all()
    assert len(harness.sent) == 2


def test_a_refused_send_does_not_reload_the_card(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])
    harness.ok = False
    harness.model.drop([one.as_uri()])
    harness.run_all()
    assert harness.notices[-1] == ("Refused.", False)
    assert harness.after_send == 0


def test_a_crashing_target_loader_ends_the_drop(tmp_path) -> None:
    (one,) = _files(tmp_path, "a.txt")
    harness = Harness([PHONE])

    def boom():
        raise RuntimeError("bus gone")

    harness.model._load_targets = boom
    harness.model.drop([one.as_uri()])
    harness.run_all()
    assert harness.model.state()["phase"] == drop.IDLE
    # The raw (English) error goes to the log only.
    assert harness.notices == [("The plugins did not answer.", False)]


def test_several_folders_are_named_briefly(tmp_path) -> None:
    folders = drop.DroppedFiles(folders=("a", "b", "c", "d"))
    assert drop.skipped_note(drop.DroppedUris(("x",)), folders) == (
        "Folders cannot be sent; skipped 4 folders (a, b, c, \u2026).")


def test_duplicate_labels_name_the_plugin() -> None:
    other = surfaces.ShareChoice("io.example.kde", "KDE Connect", "k1", "iPhone")
    rows = drop.choice_rows([PHONE, other])
    assert [row["label"] for row in rows] == ["iPhone (LocalSend)", "iPhone (KDE Connect)"]
    assert rows[0]["icon"] == "document-send"
