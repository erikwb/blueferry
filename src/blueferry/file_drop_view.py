"""Files dropped on the main window go to a "Send to…" target (Qt and GTK).

The drop zone reuses the share surface (PLUGINS.md, capability ``share``):
no new plugin API, the same ``ShareTargets``/``SendFiles`` calls as "Send
to…" on the phone card and ``blueferry send``. This module holds every
decision and string, so the Qt client (a QML ``DropArea``) and the GTK
client (a ``Gtk.DropTarget`` for ``Gdk.FileList``) only draw the overlay,
the picker and the notices:

- Only local ``file://`` URIs count; anything else (``https://``, ``sftp://``,
  a ``file://`` URI naming another host) is skipped and said so.
- Folders and other non-regular files are skipped: ``SendFiles`` takes
  regular files only, 1 to :data:`~blueferry.plugin_api.surfaces.MAX_SHARE_FILES`
  of them. Plugins declare no further size or type limits today.
- No target: explain which plugins provide one and offer Settings > Plugins.
  One target: send at once with a short notice. Several: a picker, with the
  target chosen last in this session preselected.

:class:`FileDrop` runs the blocking parts (``stat`` of the dropped paths,
``ShareTargets``, ``SendFiles``) on the client's
:class:`~blueferry.client_tasks.TaskRunner`; its state changes happen on the
UI thread. Everything returned is plain text.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from blueferry import plugin_surfaces as surfaces
from blueferry.i18n import _, ngettext
from blueferry.plugin_api.surfaces import MAX_SHARE_FILES, checked_share_paths, plain

log = logging.getLogger(__name__)

# Phases of :class:`FileDrop`.
IDLE = "idle"
CHECKING = "checking"
PICKING = "picking"
NO_TARGET = "none"
SENDING = "sending"


def overlay_title() -> str:
    return _("Drop to send to iPhone")


def overlay_subtitle() -> str:
    return _("The files go to a “Send to” target such as LocalSend.")


def no_target_title() -> str:
    return _("No Way to Send Files Yet")


def no_target_body(problems: Sequence[str] = ()) -> str:
    """Why nothing was sent, naming the plugins that did not answer."""
    body = _("Dropped files go to a plugin with “Send to” (the share "
             "capability), for example LocalSend. Install or enable one under "
             "Settings > Plugins.")
    if problems:
        body += "\n\n" + _("These plugins did not answer: {problems}").format(
            problems="; ".join(problems))
    return body


def settings_label() -> str:
    return _("Open Plugin Settings")


def picker_title() -> str:
    return _("Send To")


def picker_body(count: int) -> str:
    return ngettext("Where should the file go?", "Where should the {count} files go?",
                    count).format(count=count)


def send_label() -> str:
    return _("Send")


def busy_notice() -> str:
    return _("Still busy with the last drop; try again in a moment.")


def not_answered_notice() -> str:
    return _("The plugins did not answer.")


def changed_notice(note: str) -> str:
    """The dropped files changed before sending; ``note`` says how."""
    text = _("Nothing was sent: the dropped files changed in the meantime.")
    return f"{text} {note}" if note else text


def sending_notice(count: int, target: str) -> str:
    return ngettext("Sending a file to {target}…", "Sending {count} files to {target}…",
                    count).format(count=count, target=target)


# ---- what was dropped ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DroppedUris:
    """Local paths of the dropped ``file://`` URIs; ``remote`` counts the rest."""

    paths: tuple[str, ...] = ()
    remote: int = 0


def local_paths(uris: Iterable[object]) -> DroppedUris:
    """Keep local ``file://`` URIs (host empty or ``localhost``) as paths."""
    paths: list[str] = []
    remote = 0
    for uri in uris:
        text = str(uri or "").strip()
        if not text:
            continue
        parts = urlsplit(text)
        path = unquote(parts.path)
        if (parts.scheme.lower() != "file" or parts.netloc.lower() not in ("", "localhost")
                or not path.startswith("/") or "\x00" in path):
            remote += 1
            continue
        if path not in paths:
            paths.append(path)
    return DroppedUris(tuple(paths), remote)


@dataclass(frozen=True, slots=True)
class DroppedFiles:
    """The local paths sorted by what they are; names only for the notices."""

    files: tuple[str, ...] = ()
    folders: tuple[str, ...] = ()
    other: tuple[str, ...] = ()


def sort_paths(paths: Iterable[str]) -> DroppedFiles:
    """*Blocking* (``stat``): regular files, folders, and the rest (gone,
    sockets, devices)."""
    files: list[str] = []
    folders: list[str] = []
    other: list[str] = []
    for text in paths:
        path = Path(text)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            other.append(path.name)
            continue
        if resolved.is_dir():
            folders.append(path.name or str(path))
        elif resolved.is_file():
            if str(resolved) not in files:
                files.append(str(resolved))
        else:
            other.append(path.name)
    return DroppedFiles(tuple(files), tuple(folders), tuple(other))


def skipped_note(dropped: DroppedUris, sorted_paths: DroppedFiles) -> str:
    """One plain sentence per kind of skipped item, or ``""``."""
    notes = []
    if sorted_paths.folders:
        count = len(sorted_paths.folders)
        names = ", ".join(plain(name, 80) for name in sorted_paths.folders[:3])
        if count > 3:
            names += ", \u2026"
        # Both forms name the folders: xgettext wants the singular's
        # placeholders in the plural too.
        notes.append(ngettext("Folders cannot be sent; skipped {names}.",
                              "Folders cannot be sent; skipped {count} folders ({names}).",
                              count).format(count=count, names=names))
    if sorted_paths.other:
        count = len(sorted_paths.other)
        notes.append(ngettext("Skipped an item that is not a regular file.",
                              "Skipped {count} items that are not regular files.",
                              count).format(count=count))
    if dropped.remote:
        notes.append(ngettext("Skipped an item that is not on this computer.",
                              "Skipped {count} items that are not on this computer.",
                              dropped.remote).format(count=dropped.remote))
    return " ".join(notes)


# ---- what to do with it -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DropPlan:
    """``kind``: ``send`` (one target), ``pick`` (several), ``none`` (no
    target) or ``nothing`` (nothing sendable; ``message`` says why)."""

    kind: str
    files: tuple[str, ...] = ()
    choices: tuple[surfaces.ShareChoice, ...] = ()
    selected: str = ""
    message: str = ""
    note: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)


def plan(
    dropped: DroppedUris,
    sorted_paths: DroppedFiles,
    targets: surfaces.ShareTargets,
    last_key: str = "",
) -> DropPlan:
    note = skipped_note(dropped, sorted_paths)
    files = sorted_paths.files
    if not files:
        return DropPlan("nothing", message=note or _("There is nothing to send."))
    if len(files) > MAX_SHARE_FILES:
        limit = _("At most {limit} files can be sent at once; {count} were dropped.").format(
            limit=MAX_SHARE_FILES, count=len(files))
        return DropPlan("nothing", message=f"{limit} {note}" if note else limit)
    choices = tuple(targets.choices)
    if not choices:
        return DropPlan("none", files, note=note, problems=tuple(targets.problems))
    if len(choices) == 1:
        return DropPlan("send", files, choices, choices[0].key, note=note)
    keys = [choice.key for choice in choices]
    selected = last_key if last_key in keys else keys[0]
    return DropPlan("pick", files, choices, selected, note=note)


def choice_rows(choices: Sequence[surfaces.ShareChoice]) -> list[dict[str, str]]:
    """Plain dicts for the picker; labels are plain text."""
    return [
        {"key": choice.key, "label": surfaces.choice_label(choice, choices),
         "icon": choice.icon or "document-send"}
        for choice in choices
    ]


# ---- the flow -----------------------------------------------------------------


class FileDrop:
    """One drop at a time: check, maybe pick, send; notices go to ``notify``.

    ``notify(text, ok)`` shows a short notice (a toast or passive message).
    ``on_sent`` runs after a send the plugin accepted, e.g. to reload the
    phone card that shows the transfer. ``is_sending`` says whether the
    client's "Send to…" (``PluginSurfacesModel.sending``) is sending right
    now; a drop waits for it, so one plugin never gets two ``SendFiles`` at
    once. The last picked target is remembered for this object's lifetime,
    i.e. the client session.
    """

    def __init__(
        self,
        *,
        runner: Any,
        notify: Callable[[str, bool], None],
        on_change: Callable[[], None] = lambda: None,
        on_sent: Callable[[], None] = lambda: None,
        load_targets: Callable[[], surfaces.ShareTargets] = surfaces.load_targets,
        send: Callable[..., surfaces.Outcome] = surfaces.send,
        sort: Callable[[Iterable[str]], DroppedFiles] = sort_paths,
        is_sending: Callable[[], bool] = lambda: False,
    ) -> None:
        self._runner = runner
        self._notify = notify
        self._on_change = on_change
        self._on_sent = on_sent
        self._load_targets = load_targets
        self._send = send
        self._sort = sort
        self._is_sending = is_sending
        self._last_key = ""
        self._plan: DropPlan | None = None
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {"phase": IDLE, "choices": [], "selected": "", "title": "", "body": "",
                "count": 0}

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    @property
    def last_key(self) -> str:
        return self._last_key

    def _set(self, **values: Any) -> None:
        self._state = {**self._state, **values}
        self._on_change()

    def _reset(self) -> None:
        self._plan = None
        self._state = self._idle_state()
        self._on_change()

    @property
    def busy(self) -> bool:
        return self._state["phase"] in (CHECKING, SENDING)

    def drop(self, uris: Iterable[object]) -> None:
        """Files dropped on the window (``file://`` URIs as text)."""
        if self.busy or self._is_sending():
            self._notify(busy_notice(), False)
            return
        dropped = local_paths(uris)
        if not dropped.paths:
            self._notify(skipped_note(dropped, DroppedFiles())
                         or _("There is nothing to send."), False)
            return
        self._plan = None
        self._set(**{**self._idle_state(), "phase": CHECKING})

        def work() -> DropPlan:
            # stat() and ShareTargets (starts the plugins): never on the UI thread.
            sorted_paths = self._sort(dropped.paths)
            if not sorted_paths.files:
                return plan(dropped, sorted_paths, surfaces.ShareTargets(), self._last_key)
            return plan(dropped, sorted_paths, self._load_targets(), self._last_key)

        self._runner.submit(work, self._planned, self._failed)

    def _failed(self, message: str) -> None:
        # The raw error is English and may come from anywhere; say it plainly.
        log.warning("file drop failed: %s", plain(message, 200))
        self._reset()
        self._notify(not_answered_notice(), False)

    def _planned(self, result: DropPlan) -> None:
        if result.kind == "nothing":
            self._reset()
            self._notify(result.message, False)
            return
        if result.note:
            self._notify(result.note, False)
        self._plan = result
        if result.kind == "send":
            self._dispatch(result.choices[0], result.files)
        elif result.kind == "pick":
            self._set(phase=PICKING, choices=choice_rows(result.choices),
                      selected=result.selected, title=picker_title(),
                      body=picker_body(len(result.files)), count=len(result.files))
        else:
            self._set(phase=NO_TARGET, choices=[], selected="", title=no_target_title(),
                      body=no_target_body(result.problems), count=len(result.files))

    def pick(self, key: str) -> None:
        """The picker's answer; remembered as the preselection for later drops."""
        current = self._plan
        if self._state["phase"] != PICKING or current is None:
            return
        choice = next((item for item in current.choices if item.key == key), None)
        if choice is None:
            return
        if self._is_sending():
            self._notify(busy_notice(), False)  # the picker stays open
            return
        self._last_key = choice.key
        self._dispatch(choice, current.files)

    def cancel(self) -> None:
        """Close the picker or the explanation without sending."""
        if self._state["phase"] in (PICKING, NO_TARGET):
            self._reset()

    def _dispatch(self, choice: surfaces.ShareChoice, files: tuple[str, ...]) -> None:
        label = surfaces.choice_label(choice, self._plan.choices if self._plan else (choice,))
        self._set(**{**self._idle_state(), "phase": SENDING, "count": len(files)})
        self._notify(sending_notice(len(files), label), True)

        def work() -> surfaces.Outcome:
            # Moved, deleted or replaced since the drop: say which, in words
            # of this module, and send nothing.
            now = self._sort(files)
            if now.folders or now.other or len(now.files) != len(files):
                return surfaces.Outcome(False, changed_notice(
                    skipped_note(DroppedUris(), now)))
            try:
                paths = checked_share_paths(now.files)
            except ValueError:
                # The message names a file: not for the log.
                log.warning("dropped files no longer pass the share checks")
                return surfaces.Outcome(False, changed_notice(""))
            return self._send(choice, paths)

        def done(outcome: surfaces.Outcome) -> None:
            self._reset()
            self._notify(outcome.message, outcome.ok)
            if outcome.ok:
                self._on_sent()

        self._runner.submit(work, done, self._failed)
