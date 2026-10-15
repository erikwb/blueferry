"""Generic UI surfaces (ApiVersion 1.2): card items, share targets, popups.

Plugins never ship UI code. They describe what to show with the small value
types below, and BlueFerry renders them in every client: card items in the
phone card's "From Plugins" section, share targets under "Send to…", and
desktop popups through BlueFerry's notification policy.

The same module validates on both sides. A plugin builds :class:`CardItem`,
:class:`Action` and :class:`ShareTarget` values (bad ids raise ``ValueError``
early); the clients parse whatever arrives with the ``parse_*`` functions,
which never trust the plugin: strings become one line of plain text cut to
the limits below, malformed entries are dropped, and only the top-level
shape raises :class:`SurfaceError`.

ApiVersion 1.4 lets an action send files: ``Action(..., send_to=TARGET)``
on a card item of a plugin that also has the ``share`` capability. The
clients then ask for files (a file dialog, a path prompt, the CLI's
arguments) and call ``SendFiles(TARGET, paths)`` instead of
``InvokeAction``; the Qt card also accepts files dropped on that item.
Older clients ignore ``send_to`` and call ``InvokeAction`` as before, so
the plugin should answer that with a hint ("use Send to…").

ApiVersion 1.5 keeps popups off the bus: the plugin queues a
:class:`Notification`, emits the content-free ``NotifyChanged()`` and the
host fetches it with ``GetPendingNotification()`` (:func:`pending_json`,
:func:`parse_pending`). Every other process on the session bus sees only
that something is waiting.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_CARD_ITEMS = 8
MAX_ACTIONS = 3
MAX_TITLE = 80
MAX_SUBTITLE = 160
MAX_LABEL = 40
MAX_MESSAGE = 200
MAX_NOTIFY_BODY = 280
MAX_SHARE_TARGETS = 16
MAX_SHARE_FILES = 64
MAX_ARGS_BYTES = 4096
MAX_URI = 2048
# The item id InvokeAction gets for a click on a popup's action button.
NOTIFY_ITEM_ID = "notify"
# Popups a plugin keeps until the host fetches them (ApiVersion 1.5): at
# most this many, for at most a minute; a stale popup is worse than none.
MAX_PENDING_NOTIFICATIONS = 8
PENDING_NOTIFICATION_TTL_SEC = 60.0
ACTION_KINDS = ("button", "primary")

# No ":": the clients address actions as PLUGIN:ITEM:ACTION.
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Freedesktop icon names: no paths, no URLs.
_ICON = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")


class SurfaceError(ValueError):
    """A reply whose overall shape is wrong; the client shows it as an error."""


def plain(value: object, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters."""
    if value is None:
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


def valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def icon_name(value: object) -> str:
    """A freedesktop icon name, or ``""`` for anything else."""
    return value if isinstance(value, str) and _ICON.fullmatch(value) else ""


def _require_id(value: str, what: str) -> str:
    if not valid_id(value):
        raise ValueError(f"{what} must match [A-Za-z0-9_.-]{{1,64}}: {value!r}")
    return value


# ---- values a plugin builds ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """A button on a card item. ``kind="primary"`` is the item's main action.

    ``send_to`` (ApiVersion 1.4) names one of the plugin's share targets:
    the client picks files and calls ``SendFiles(send_to, paths)`` instead
    of ``InvokeAction``.
    """

    id: str
    label: str
    icon: str | None = None
    kind: str = "button"
    send_to: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.id, "Action.id")
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"Action.kind must be one of {ACTION_KINDS}")
        if self.send_to is not None:
            _require_id(self.send_to, "Action.send_to")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id, "label": plain(self.label, MAX_LABEL),
            "icon": icon_name(self.icon) or None, "kind": self.kind,
        }
        if self.send_to is not None:
            # Only when set: a 1.3 host that checks the exact keys still
            # accepts every other action.
            data["send_to"] = self.send_to
        return data


@dataclass(frozen=True, slots=True)
class CardItem:
    """One row in the phone card's "From Plugins" section."""

    id: str
    title: str
    icon: str = ""
    subtitle: str | None = None
    actions: Sequence[Action] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_id(self.id, "CardItem.id")
        if len(self.actions) > MAX_ACTIONS:
            raise ValueError(f"a card item has at most {MAX_ACTIONS} actions")

    @property
    def send_target(self) -> str | None:
        """The share target files dropped on this item go to (ApiVersion 1.4):
        the ``send_to`` of its first sending action."""
        return next((action.send_to for action in self.actions if action.send_to), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "icon": icon_name(self.icon),
            "title": plain(self.title, MAX_TITLE),
            "subtitle": plain(self.subtitle, MAX_SUBTITLE) if self.subtitle else None,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class ShareTarget:
    """A destination under "Send to…", e.g. "iPhone (LocalSend)"."""

    id: str
    label: str
    icon: str = ""

    def __post_init__(self) -> None:
        _require_id(self.id, "ShareTarget.id")

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "label": plain(self.label, MAX_LABEL), "icon": icon_name(self.icon)}


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Answer to InvokeAction. ``open_uri``: file:// in the plugin cache or http(s)."""

    ok: bool
    message: str | None = None
    open_uri: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": bool(self.ok),
            "message": plain(self.message, MAX_MESSAGE) if self.message else None,
            "open_uri": self.open_uri or None,
        }


@dataclass(frozen=True, slots=True)
class SendResult:
    """Answer to SendFiles; long transfers report progress on a card item."""

    ok: bool
    message: str | None = None
    job: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": bool(self.ok),
            "message": plain(self.message, MAX_MESSAGE) if self.message else None,
            "job": self.job if valid_id(self.job) else None,
        }


@dataclass(frozen=True, slots=True)
class Notification:
    """A validated popup as the host shows it."""

    title: str
    body: str
    icon: str = ""
    action_label: str = ""
    action_id: str = ""

    @property
    def has_action(self) -> bool:
        return bool(self.action_label and self.action_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title, "body": self.body, "icon": self.icon,
            "action_label": self.action_label, "action_id": self.action_id,
        }


def _as_dict(value: object) -> Mapping[str, object]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return value
    raise TypeError(f"expected a surface value or a dict, got {type(value).__name__}")


def card_items_json(items: Iterable[object]) -> str:
    """The GetCardItems reply for ``CardItem`` values (or plain dicts)."""
    return json.dumps({"items": [dict(_as_dict(item)) for item in list(items)[:MAX_CARD_ITEMS]]})


def share_targets_json(targets: Iterable[object]) -> str:
    return json.dumps(
        {"targets": [dict(_as_dict(target)) for target in list(targets)[:MAX_SHARE_TARGETS]]}
    )


def result_json(result: object) -> str:
    """An ActionResult/SendResult (or a dict with the same keys) as JSON."""
    return json.dumps(dict(_as_dict(result)))


# ---- parsing on the BlueFerry side ---------------------------------------------


def _decode(text: object) -> object:
    if isinstance(text, (str, bytes)):
        try:
            return json.loads(text)
        except ValueError:
            raise SurfaceError("the plugin sent invalid JSON") from None
    return text


def _action(entry: object) -> Action | None:
    if not isinstance(entry, Mapping) or not valid_id(entry.get("id")):
        return None
    label = plain(entry.get("label"), MAX_LABEL)
    if not label:
        return None
    kind = entry.get("kind")
    send_to = entry.get("send_to")
    return Action(
        id=str(entry["id"]), label=label, icon=icon_name(entry.get("icon")) or None,
        kind=str(kind) if kind in ACTION_KINDS else "button",
        send_to=str(send_to) if valid_id(send_to) else None,
    )


def _item(entry: object) -> CardItem | None:
    if not isinstance(entry, Mapping) or not valid_id(entry.get("id")):
        return None
    title = plain(entry.get("title"), MAX_TITLE)
    if not title:
        return None
    raw_actions = entry.get("actions")
    actions: list[Action] = []
    seen: set[str] = set()
    for raw in raw_actions if isinstance(raw_actions, list) else ():
        action = _action(raw)
        if action is not None and action.id not in seen:
            seen.add(action.id)
            actions.append(action)
        if len(actions) == MAX_ACTIONS:
            break
    return CardItem(
        id=str(entry["id"]), title=title, icon=icon_name(entry.get("icon")),
        subtitle=plain(entry.get("subtitle"), MAX_SUBTITLE) or None, actions=tuple(actions),
    )


def parse_card_items(reply: object) -> list[CardItem]:
    """GetCardItems: at most 8 valid items with at most 3 actions each."""
    value = _decode(reply)
    raw = value.get("items") if isinstance(value, Mapping) else None
    if not isinstance(raw, list):
        raise SurfaceError("the plugin sent an invalid card")
    items: list[CardItem] = []
    seen: set[str] = set()
    for entry in raw:
        item = _item(entry)
        if item is not None and item.id not in seen:
            seen.add(item.id)
            items.append(item)
        if len(items) == MAX_CARD_ITEMS:
            break
    return items


def parse_share_targets(reply: object) -> list[ShareTarget]:
    value = _decode(reply)
    raw = value.get("targets") if isinstance(value, Mapping) else None
    if not isinstance(raw, list):
        raise SurfaceError("the plugin sent an invalid target list")
    targets: list[ShareTarget] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping) or not valid_id(entry.get("id")):
            continue
        label = plain(entry.get("label"), MAX_LABEL)
        if not label or entry["id"] in seen:
            continue
        seen.add(str(entry["id"]))
        targets.append(ShareTarget(str(entry["id"]), label, icon_name(entry.get("icon"))))
        if len(targets) == MAX_SHARE_TARGETS:
            break
    return targets


def _result_fields(reply: object) -> Mapping[str, object]:
    value = _decode(reply)
    if not isinstance(value, Mapping) or not isinstance(value.get("ok"), bool):
        raise SurfaceError("the plugin sent an invalid answer")
    return value


def parse_action_result(reply: object) -> ActionResult:
    """InvokeAction; ``open_uri`` still needs :func:`checked_open_uri`."""
    value = _result_fields(reply)
    uri = value.get("open_uri")
    return ActionResult(
        ok=bool(value["ok"]),
        message=plain(value.get("message"), MAX_MESSAGE) or None,
        open_uri=uri if isinstance(uri, str) and 0 < len(uri) <= MAX_URI else None,
    )


def parse_send_result(reply: object) -> SendResult:
    value = _result_fields(reply)
    job = value.get("job")
    return SendResult(
        ok=bool(value["ok"]),
        message=plain(value.get("message"), MAX_MESSAGE) or None,
        job=str(job) if valid_id(job) else None,
    )


def parse_notification(
    title: object, body: object, icon: object = "", action_label: object = "",
    action_id: object = "",
) -> Notification | None:
    """A popup's fields, cleaned; None when there is nothing to show."""
    clean_title = plain(title, MAX_TITLE)
    clean_body = plain(body, MAX_NOTIFY_BODY)
    if not clean_title and not clean_body:
        return None
    label = plain(action_label, MAX_LABEL)
    action = str(action_id) if valid_id(action_id) else ""
    return Notification(
        title=clean_title, body=clean_body, icon=icon_name(icon),
        action_label=label if action else "", action_id=action if label else "",
    )


def pending_json(note: Notification | None, *, more: bool = False) -> str:
    """GetPendingNotification's reply: the oldest waiting popup or null."""
    return json.dumps({"notification": note.to_dict() if note is not None else None,
                       "more": bool(more)})


def parse_pending(reply: object) -> tuple[Notification | None, bool]:
    """``(popup or None, more waiting)`` from GetPendingNotification.

    The popup's fields go through :func:`parse_notification` again: the
    host never trusts what the plugin sends.
    """
    value = _decode(reply)
    if not isinstance(value, Mapping) or not isinstance(value.get("more", False), bool):
        raise SurfaceError("the plugin sent an invalid pending notification")
    note = value.get("notification")
    more = bool(value.get("more", False))
    if note is None:
        return None, more
    if not isinstance(note, Mapping):
        raise SurfaceError("the plugin sent an invalid pending notification")
    return parse_notification(
        note.get("title", ""), note.get("body", ""), note.get("icon", ""),
        note.get("action_label", ""), note.get("action_id", ""),
    ), more


def checked_open_uri(
    uri: object, cache_roots: Sequence[Path], *, uid: int | None = None,
) -> str | None:
    """``open_uri`` if BlueFerry may open it, else None.

    Allowed: ``http(s)://`` URLs, and ``file://`` URIs of a file or folder
    owned by the user below one of ``cache_roots`` (symlinks resolved).
    """
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI:
        return None
    if any(not ch.isprintable() or ch.isspace() for ch in uri):
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        return uri if parts.netloc and not parts.username and not parts.password else None
    if scheme != "file" or parts.netloc not in ("", "localhost") or parts.query or parts.fragment:
        return None
    path = Path(unquote(parts.path))
    if not path.is_absolute() or "\x00" in str(path):
        return None
    try:
        resolved = path.resolve(strict=True)
        roots = [Path(root).resolve() for root in cache_roots]
        info = os.stat(resolved)
    except (OSError, RuntimeError):
        return None
    if not any(root in resolved.parents for root in roots):
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        return None
    if info.st_uid != (os.getuid() if uid is None else uid):
        return None
    return resolved.as_uri()


def checked_share_paths(paths: Iterable[object]) -> list[str]:
    """Absolute paths of existing regular files, at most 64; else ValueError."""
    result: list[str] = []
    for value in paths:
        text = os.fspath(value) if isinstance(value, (str, os.PathLike)) else ""
        if not text or "\x00" in text:
            raise ValueError("not a file path")
        path = Path(text).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError(f"no such file: {plain(path.name, 80)}") from None
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {plain(path.name, 80)}")
        if str(resolved) not in result:
            result.append(str(resolved))
    if not result:
        raise ValueError("no files to send")
    if len(result) > MAX_SHARE_FILES:
        raise ValueError(f"at most {MAX_SHARE_FILES} files at once")
    return result


def args_json(args: Mapping[str, object] | None) -> str:
    text = json.dumps(dict(args or {}))
    if len(text.encode("utf-8")) > MAX_ARGS_BYTES:
        raise ValueError("action arguments are too large")
    return text
