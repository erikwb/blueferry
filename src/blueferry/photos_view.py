"""Toolkit-neutral access to a ``photos`` plugin for every client.

Functions marked *blocking* call the plugin over D-Bus (with timeouts) and
belong on a worker thread or in the CLI. Anything coming from the plugin is
already validated by :class:`~blueferry.plugin_api.client.PluginClient`;
labels built here are plain text.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from blueferry import __version__
from blueferry.i18n import _
from blueferry.plugin_api import CAPABILITY_PHOTOS
from blueferry.plugin_api.client import Photo, PluginClient, PluginError
from blueferry.plugin_api.manifest import Discovery, PluginManifest, discover

SETUP_COMMAND = "blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich"
DEFAULT_LIMIT = 60
_TYPE_TEXT = {"image": _("Photo"), "video": _("Video"), "other": _("File")}


def find_plugin(
    found: Discovery | None = None, disabled: frozenset[str] | None = None,
) -> PluginManifest | None:
    """The first enabled plugin with the ``photos`` capability (no I/O but files)."""
    found = found if found is not None else discover(blueferry_version=__version__)
    if disabled is None:
        from blueferry.plugin_manager import PluginManager

        disabled = PluginManager().disabled()
    plugins = [p for p in found.with_capability(CAPABILITY_PHOTOS) if p.id not in disabled]
    return plugins[0] if plugins else None


def not_installed_hint() -> str:
    return _(
        "No photo plugin is installed. Add Immich photos in Settings > Plugins, or run: {command}"
    ).format(
        command=SETUP_COMMAND,
    )


@dataclass(frozen=True, slots=True)
class PhotosSnapshot:
    present: bool
    ready: bool
    hint: str
    photos: list[Photo] = field(default_factory=list)


def load_recent(
    manifest: PluginManifest | None,
    limit: int = DEFAULT_LIMIT,
    *,
    client_factory: Callable[[PluginManifest], PluginClient] = PluginClient,
) -> PhotosSnapshot:
    """*Blocking.* Status plus the newest photos; never raises PluginError."""
    if manifest is None:
        return PhotosSnapshot(False, False, not_installed_hint())
    client = client_factory(manifest)
    try:
        status = client.status()
        if status.state == "unconfigured":
            hint = status.detail or _("Configure the plugin in Settings > Plugins.")
            return PhotosSnapshot(True, False, hint)
        photos = client.list_recent(limit)
    except PluginError as error:
        return PhotosSnapshot(True, False, _("Photos unavailable: {reason}").format(reason=error))
    hint = _("{count} recent items from {server}").format(
        count=len(photos), server=status.server or manifest.name,
    )
    return PhotosSnapshot(True, True, hint, photos)


def fetch_original(
    manifest: PluginManifest,
    photo_id: str,
    *,
    client_factory: Callable[[PluginManifest], PluginClient] = PluginClient,
) -> Path:
    """*Blocking.* Download (or reuse) the original; raises PluginError."""
    return client_factory(manifest).fetch_original(photo_id)


def load_current() -> tuple[PluginManifest | None, PhotosSnapshot]:
    """*Blocking.* Look the photo plugin up again and load its newest items."""
    manifest = find_plugin()
    return manifest, load_recent(manifest)


def fetch_current_original(plugin_id: str, photo_id: str) -> Path | None:
    """*Blocking.* The original, but only from the plugin the list came from.

    ``None`` when that plugin was disabled, removed or replaced since the
    list loaded: it is never called then, because a call would start a
    disabled plugin again by D-Bus activation. Raises PluginError.
    """
    current = find_plugin()
    if current is None or current.id != plugin_id:
        return None
    return fetch_original(current, photo_id)


# The Photos tab's state map, the same for every client. ``url`` turns a
# local path into the client's file URL ("" for none).

def initial_state() -> dict[str, object]:
    return {"present": False, "ready": False, "loaded": False, "hint": "", "items": []}


def photo_item(photo: Photo, url: Callable[[object], str]) -> dict[str, object]:
    """One grid cell; paths become file URLs, text stays plain."""
    return {
        "id": photo.id,
        "label": label(photo),
        "video": photo.type == "video",
        "thumbnail": url(photo.thumbnail),
        "original": url(photo.original),
    }


def loaded_state(snapshot: PhotosSnapshot, url: Callable[[object], str]) -> dict[str, object]:
    return {
        "present": snapshot.present,
        "ready": snapshot.ready,
        "loaded": True,
        "hint": snapshot.hint,
        "items": [photo_item(photo, url) for photo in snapshot.photos],
    }


def failed_state(present: bool, message: str) -> dict[str, object]:
    """A load that failed: never leave the tab spinning, show why instead."""
    return {"present": present, "ready": False, "loaded": True,
            "hint": message or _("Photos unavailable"), "items": []}


def with_original(state: dict[str, Any], photo_id: str, original: str) -> dict[str, Any]:
    """The state after one original was downloaded (``original`` is its URL)."""
    items = [
        {**item, "original": original} if item["id"] == photo_id else item
        for item in state["items"]
    ]
    return {**state, "items": items}


def taken_text(photo: Photo) -> str:
    """Local date and time, or an empty string for an unknown timestamp."""
    if not photo.taken_at:
        return ""
    try:
        moment = datetime.fromisoformat(photo.taken_at.replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError):
        return ""


def type_text(photo: Photo) -> str:
    return _TYPE_TEXT.get(photo.type, _TYPE_TEXT["other"])


def label(photo: Photo) -> str:
    when = taken_text(photo)
    return f"{when}  {type_text(photo)}" if when else type_text(photo)
