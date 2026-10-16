"""Client side of the plugin contract: talk to one plugin, distrust it.

Calls are synchronous dbus-python calls with explicit timeouts; clients run
them on a worker thread (Qt task, Textual worker) or in the CLI, never on a
UI thread. Every reply is size-limited, parsed as JSON and validated; paths
a plugin hands out must be owner-only regular files inside the plugin's cache
directory. Nothing a plugin returns is rendered as markup.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import (
    LOGIN_PROVIDERS,
    MAX_RECENT_PHOTOS,
    MAX_REPLY_BYTES,
    METHOD_CONFIG_LOGIN,
    METHOD_CONFIG_LOGIN_CANCEL,
    METHOD_CONFIG_LOGIN_STATUS,
    METHOD_GET_CARD_ITEMS,
    METHOD_GET_PENDING_NOTIFICATION,
    METHOD_INVOKE_ACTION,
    METHOD_SEND_FILES,
    METHOD_SHARE_TARGETS,
    METHOD_TEST_CONFIG,
    OBJECT_PATH,
    PHOTOS_INTERFACE,
    PLUGIN_INTERFACE,
    config_flow,
    surfaces,
)
from .config import SECRET_MASK, ConfigError
from .manifest import PluginManifest

STATUS_TIMEOUT_SEC = 10.0
LIST_TIMEOUT_SEC = 90.0
FETCH_TIMEOUT_SEC = 600.0
# SetConfig may check the new settings against the plugin's server.
CONFIG_TIMEOUT_SEC = 60.0
# ApiVersion 1.3: TestConfig talks to the server; ConfigLogin only starts a
# browser flow and ConfigLoginStatus answers from the plugin's own state.
TEST_TIMEOUT_SEC = 60.0
LOGIN_TIMEOUT_SEC = 30.0
# ApiVersion 1.2 surfaces. SendFiles only starts a transfer; long ones
# report progress on a card item.
CARD_TIMEOUT_SEC = 15.0
ACTION_TIMEOUT_SEC = 60.0
SHARE_TIMEOUT_SEC = 60.0
_ASSET_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TIMESTAMP = re.compile(r"^[0-9T:.+\-Z ]{4,40}$")
_PHOTO_TYPES = frozenset({"image", "video", "other"})
_STATES = frozenset({"ok", "unconfigured", "error", "busy"})
_TOKEN = re.compile(r"^[a-z0-9-]{1,40}$")


class PluginError(Exception):
    """A plugin is missing, misbehaving or reported an error."""


class Transport(Protocol):
    def call(
        self, bus_name: str, interface: str, method: str, signature: str,
        args: tuple, timeout: float,
    ) -> object: ...

    def owner_uid(self, bus_name: str) -> int | None: ...


class DBusTransport:
    """Session-bus transport; a plugin is reachable only on the user's bus."""

    def __init__(self, bus: Any = None) -> None:
        self._bus = bus

    def _connection(self) -> Any:
        if self._bus is None:
            import dbus

            self._bus = dbus.SessionBus()
        return self._bus

    def call(
        self, bus_name: str, interface: str, method: str, signature: str,
        args: tuple, timeout: float,
    ) -> object:
        import dbus.exceptions

        bus = self._connection()
        try:
            # Activation happens here: the bus starts the plugin from its
            # D-Bus service file when the name has no owner yet.
            proxy = bus.get_object(bus_name, OBJECT_PATH, introspect=False)
            return getattr(proxy, method)(
                *args, dbus_interface=interface, signature=signature, timeout=timeout,
            )
        except dbus.exceptions.DBusException as error:
            raise PluginError(_error_text(error)) from None

    def owner_uid(self, bus_name: str) -> int | None:
        import dbus.exceptions

        try:
            return int(self._connection().get_unix_user(bus_name))
        except dbus.exceptions.DBusException:
            return None


def _error_text(error: Any) -> str:
    name = str(error.get_dbus_name() or "")
    if name in (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
    ):
        return "the plugin is not installed or could not be started"
    if name == "org.freedesktop.DBus.Error.NoReply":
        return "the plugin did not answer in time"
    message = str(error.get_dbus_message() or name)
    return plain_text(message, 200) or "the plugin failed"


def plain_text(value: object, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


@dataclass(frozen=True, slots=True)
class PluginStatus:
    state: str
    detail: str = ""
    server: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ok"


@dataclass(frozen=True, slots=True)
class ConfigResult:
    ok: bool
    # field key (or "" for the whole form) -> short plain-text reason
    errors: dict[str, str]


@dataclass(frozen=True, slots=True)
class Photo:
    id: str
    taken_at: str
    type: str
    thumbnail: Path | None
    original: Path | None = None


class PluginClient:
    """One plugin's Plugin1 (and capability) methods, validated."""

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        transport: Transport | None = None,
        uid: int | None = None,
        cache_root: Callable[[], Path] | None = None,
    ) -> None:
        self.manifest = manifest
        self._transport = transport or DBusTransport()
        self._uid = os.getuid() if uid is None else uid
        self._cache_roots: Callable[[], tuple[Path, ...]] = (
            (lambda: (cache_root(),)) if cache_root is not None else default_cache_roots
        )

    def _call(self, interface: str, method: str, signature: str, args: tuple,
              timeout: float, *, precheck: bool = False) -> str:
        if precheck:
            # Before handing over files or a click: refuse a running plugin
            # owned by somebody else. Without an owner yet, the call below
            # activates ours and the check after it applies.
            owner = self._transport.owner_uid(self.manifest.bus_name)
            if owner is not None and owner != self._uid:
                raise PluginError("the plugin runs as a different user; ignoring it")
        reply = self._transport.call(
            self.manifest.bus_name, interface, method, signature, args, timeout,
        )
        # The name may only now have an owner (activation). Refuse a plugin
        # run by somebody else, even on a shared bus.
        owner = self._transport.owner_uid(self.manifest.bus_name)
        if owner != self._uid:
            raise PluginError("the plugin runs as a different user; ignoring it")
        if not isinstance(reply, str):
            raise PluginError("the plugin sent an invalid reply")
        if len(reply.encode("utf-8", "surrogatepass")) > MAX_REPLY_BYTES:
            raise PluginError("the plugin sent an oversized reply")
        return str(reply)

    def _json(self, interface: str, method: str, signature: str, args: tuple,
              timeout: float) -> object:
        try:
            return json.loads(self._call(interface, method, signature, args, timeout))
        except ValueError:
            raise PluginError("the plugin sent invalid JSON") from None

    def status(self) -> PluginStatus:
        value = self._json(PLUGIN_INTERFACE, "Status", "", (), STATUS_TIMEOUT_SEC)
        if not isinstance(value, Mapping):
            raise PluginError("the plugin sent an invalid status")
        state = value.get("state")
        if state not in _STATES:
            raise PluginError("the plugin sent an invalid status")
        return PluginStatus(
            state=str(state),
            detail=plain_text(value.get("detail", ""), 200),
            server=plain_text(value.get("server", ""), 100),
        )

    # ---- settings -------------------------------------------------------------

    def get_config(self) -> dict[str, object]:
        """Current settings for the manifest's fields; secrets only as a mask."""
        fields = self.manifest.config
        if not fields:
            raise PluginError("this plugin has no settings")
        value = self._json(PLUGIN_INTERFACE, "GetConfig", "", (), STATUS_TIMEOUT_SEC)
        values = value.get("values") if isinstance(value, Mapping) else None
        if not isinstance(values, Mapping):
            raise PluginError("the plugin sent invalid settings")
        result: dict[str, object] = {}
        for field in fields:
            raw = values.get(field.key)
            if field.secret:
                # Whatever a plugin sends for a secret, show only whether one is set.
                result[field.key] = SECRET_MASK if raw else ""
                continue
            try:
                result[field.key] = field.empty() if raw in (None, "") else field.coerce(raw)
            except ConfigError:
                result[field.key] = field.empty()
        return result

    def _update(self, values: Mapping[str, object], *, secrets: bool = True) -> dict[str, object]:
        fields = {field.key: field for field in self.manifest.config}
        if not fields:
            raise PluginError("this plugin has no settings")
        update: dict[str, object] = {}
        for key, value in values.items():
            field = fields.get(key)
            if field is None:
                continue
            if field.secret and (not secrets or value in (None, "", SECRET_MASK)):
                continue
            update[key] = value
        return update

    def set_config(self, values: Mapping[str, object]) -> ConfigResult:
        """Send changed settings; a secret only when a new one was typed."""
        update = self._update(values)
        reply = self._json(
            PLUGIN_INTERFACE, "SetConfig", "s", (json.dumps(update),), CONFIG_TIMEOUT_SEC,
        )
        if not isinstance(reply, Mapping) or not isinstance(reply.get("ok"), bool):
            raise PluginError("the plugin sent an invalid answer")
        errors = reply.get("errors")
        reasons = {
            plain_text(key, 32): plain_text(reason, 200)
            for key, reason in (errors.items() if isinstance(errors, Mapping) else ())
        }
        if not reply["ok"] and not reasons:
            reasons = {"": "the plugin rejected the settings"}
        return ConfigResult(ok=bool(reply["ok"]), errors=reasons)

    # ---- settings helpers (ApiVersion 1.3) ------------------------------------------

    def test_config(self, values: Mapping[str, object]) -> config_flow.ConfigTestResult:
        """TestConfig: check typed values without saving them."""
        if not self.manifest.config_test:
            raise PluginError("this plugin cannot test its settings")
        update = self._update(values)
        return self._flow(
            config_flow.parse_config_test, METHOD_TEST_CONFIG, "s", (json.dumps(update),),
            TEST_TIMEOUT_SEC,
        )

    def login_provider(self) -> str:
        """The manifest's ConfigLogin provider if this client knows it, else ""."""
        provider = self.manifest.config_login
        return provider if provider in LOGIN_PROVIDERS else ""

    def config_login(self, values: Mapping[str, object]) -> config_flow.LoginStep:
        """ConfigLogin: start the browser sign-in with the typed non-secret
        values. An ``open`` step carries a checked https ``open_uri``."""
        provider = self.login_provider()
        if not provider:
            raise PluginError("this plugin has no browser sign-in")
        payload = config_flow.form_values_json(self._update(values, secrets=False))
        return self._flow(
            lambda reply: config_flow.parse_login_step(reply, start=True),
            METHOD_CONFIG_LOGIN, "ss", (provider, payload), LOGIN_TIMEOUT_SEC, precheck=True,
        )

    def config_login_status(self, login_id: str) -> config_flow.LoginStep:
        if not config_flow.valid_login_id(login_id):
            raise PluginError("not a sign-in id")
        return self._flow(
            lambda reply: config_flow.parse_login_step(reply, start=False),
            METHOD_CONFIG_LOGIN_STATUS, "s", (login_id,), LOGIN_TIMEOUT_SEC,
        )

    def config_login_cancel(self, login_id: str) -> None:
        if not config_flow.valid_login_id(login_id):
            raise PluginError("not a sign-in id")
        self._call(PLUGIN_INTERFACE, METHOD_CONFIG_LOGIN_CANCEL, "s", (login_id,),
                   LOGIN_TIMEOUT_SEC)

    def _flow(self, parse: Callable[[object], Any], method: str, signature: str, args: tuple,
              timeout: float, *, precheck: bool = False) -> Any:
        reply = self._call(PLUGIN_INTERFACE, method, signature, args, timeout,
                           precheck=precheck)
        try:
            return parse(reply)
        except config_flow.FlowError as error:
            raise PluginError(str(error)) from None

    # ---- surfaces (ApiVersion 1.2) ------------------------------------------------

    def _surface(self, parse: Callable[[object], Any], interface: str, method: str,
                 signature: str, args: tuple, timeout: float, *,
                 precheck: bool = False) -> Any:
        reply = self._call(interface, method, signature, args, timeout, precheck=precheck)
        try:
            return parse(reply)
        except surfaces.SurfaceError as error:
            raise PluginError(str(error)) from None

    def card_items(self) -> list[surfaces.CardItem]:
        """GetCardItems: at most 8 items, 3 actions each, plain text."""
        return self._surface(
            surfaces.parse_card_items, PLUGIN_INTERFACE, METHOD_GET_CARD_ITEMS, "", (),
            CARD_TIMEOUT_SEC,
        )

    def invoke_action(
        self, item_id: str, action_id: str, args: Mapping[str, object] | None = None,
        *, notify: bool = False,
    ) -> surfaces.ActionResult:
        """InvokeAction for a card item, or with ``notify=True`` for a popup
        button (item id ``"notify"``).

        An ``open_uri`` that is neither http(s) nor a file in the plugin
        cache is dropped.
        """
        if notify:
            item_id = surfaces.NOTIFY_ITEM_ID
        if not surfaces.valid_id(item_id) or not surfaces.valid_id(action_id):
            raise PluginError("not an action id")
        try:
            payload = surfaces.args_json(args)
        except (TypeError, ValueError):
            raise PluginError("invalid action arguments") from None
        result = self._surface(
            surfaces.parse_action_result, PLUGIN_INTERFACE, METHOD_INVOKE_ACTION, "sss",
            (item_id, action_id, payload), ACTION_TIMEOUT_SEC, precheck=True,
        )
        uri = self.checked_open_uri(result.open_uri) if result.open_uri else None
        return surfaces.ActionResult(result.ok, result.message, uri)

    def share_targets(self) -> list[surfaces.ShareTarget]:
        return self._surface(
            surfaces.parse_share_targets, PLUGIN_INTERFACE, METHOD_SHARE_TARGETS, "", (),
            CARD_TIMEOUT_SEC,
        )

    def send_files(self, target_id: str, paths: list[str]) -> surfaces.SendResult:
        """SendFiles with absolute paths of existing regular files."""
        if not surfaces.valid_id(target_id):
            raise PluginError("not a share target")
        try:
            files = surfaces.checked_share_paths(paths)
        except ValueError as error:
            raise PluginError(str(error)) from None
        return self._surface(
            surfaces.parse_send_result, PLUGIN_INTERFACE, METHOD_SEND_FILES, "sas",
            (target_id, files), SHARE_TIMEOUT_SEC, precheck=True,
        )

    def pending_notification(self) -> tuple[surfaces.Notification | None, bool]:
        """GetPendingNotification (1.5): the oldest waiting popup and
        whether more wait. BlueFerry's daemon does the same asynchronously."""
        return self._surface(
            surfaces.parse_pending, PLUGIN_INTERFACE, METHOD_GET_PENDING_NOTIFICATION, "", (),
            CARD_TIMEOUT_SEC,
        )

    def checked_open_uri(self, uri: object) -> str | None:
        """Only http(s), or a file of this plugin's own cache directory."""
        roots = [
            root / name for root in self._cache_roots()
            for name in (self.manifest.id, self.manifest.alias) if name
        ]
        return surfaces.checked_open_uri(uri, roots, uid=self._uid)

    # ---- photos capability ---------------------------------------------------

    def list_recent(self, limit: int) -> list[Photo]:
        bounded = max(1, min(int(limit), MAX_RECENT_PHOTOS))
        value = self._json(
            PHOTOS_INTERFACE, "ListRecent", "u", (bounded,), LIST_TIMEOUT_SEC,
        )
        if not isinstance(value, list):
            raise PluginError("the plugin sent an invalid photo list")
        photos = []
        seen: set[str] = set()
        for entry in value[:bounded]:
            photo = self._photo(entry)
            if photo is not None and photo.id not in seen:
                seen.add(photo.id)
                photos.append(photo)
        return photos

    def fetch_original(self, photo_id: str) -> Path:
        if not _ASSET_ID.fullmatch(photo_id):
            raise PluginError("not a photo id")
        path = self.checked_path(
            self._call(PHOTOS_INTERFACE, "FetchOriginal", "s", (photo_id,), FETCH_TIMEOUT_SEC)
        )
        if path is None:
            raise PluginError("the plugin returned an unusable file")
        return path

    def _photo(self, entry: object) -> Photo | None:
        if not isinstance(entry, Mapping):
            return None
        photo_id, taken_at, kind = entry.get("id"), entry.get("taken_at"), entry.get("type")
        if not isinstance(photo_id, str) or not _ASSET_ID.fullmatch(photo_id):
            return None
        if not isinstance(taken_at, str) or not _TIMESTAMP.fullmatch(taken_at):
            taken_at = ""
        if kind not in _PHOTO_TYPES:
            kind = "other"
        return Photo(
            id=photo_id,
            taken_at=str(taken_at),
            type=str(kind),
            thumbnail=self.checked_path(entry.get("thumbnail")),
            original=self.checked_path(entry.get("original")),
        )

    def checked_path(self, value: object) -> Path | None:
        """A plugin-supplied path, only if it is our own file in our cache."""
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            return None
        path = Path(value)
        if not path.is_absolute():
            return None
        try:
            resolved = path.resolve(strict=True)
            roots = [root.resolve() for root in self._cache_roots()]
            info = os.lstat(resolved)
        except (OSError, RuntimeError):
            return None
        if not any(root in resolved.parents for root in roots):
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid:
            return None
        return resolved


def plugin_cache_roots(plugin_id: str, alias: str = "") -> tuple[Path, ...]:
    """A plugin's own cache directories: ``<cache root>/<id>`` and, with an
    alias, ``<cache root>/<alias>`` (e.g. ``~/.cache/blueferry/immich``)."""
    return tuple(
        root / name for root in default_cache_roots() for name in (plugin_id, alias) if name
    )


def default_cache_roots() -> tuple[Path, ...]:
    """Where plugin files may live: ``$XDG_CACHE_HOME/blueferry``, and
    ``~/.cache/blueferry`` because a bus-activated plugin inherits the bus
    daemon's environment, which may lack the client's XDG_CACHE_HOME."""
    roots = [Path(os.path.expanduser("~")) / ".cache" / "blueferry"]
    if os.environ.get("XDG_CACHE_HOME"):
        roots.insert(0, Path(os.environ["XDG_CACHE_HOME"]) / "blueferry")
    return tuple(dict.fromkeys(roots))
