"""Asynchronous presentation controller for the Kirigami client."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable

from PySide6.QtCore import (
    SLOT,
    Property,
    QObject,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtDBus import QDBusConnection

from blueferry import __version__, phone_audio_view, phone_overview, photos_view
from blueferry.backend_lifecycle import ensure_backend_current, restart_backend
from blueferry.bluetooth_devices import iphone_candidates
from blueferry.client import BackendClient
from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
    fetch_conversation_snapshot,
)
from blueferry.doctor_report import DoctorReport, run_doctor
from blueferry.i18n import _
from blueferry.models import BackendStatus, CallsSnapshot
from blueferry.onboarding import OnboardingState, effective_compatibility
from blueferry.plugin_api.client import Photo
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH
from blueferry.qt import phone_link
from blueferry.qt.avatars import avatar_url
from blueferry.qt.companion import CompanionTools
from blueferry.qt.plugin_settings import PluginSettings
from blueferry.qt.plugin_surfaces import PluginSurfaces
from blueferry.qt.tasks import Task
from blueferry.quirks_report import issue_report, issue_url
from blueferry.reconnect_view import UNREACHABLE_TEXT, reconnect_view, result_text
from blueferry.service_manager import bluetooth_restart_command
from blueferry.setup_client import (
    DISCOVERY_SECONDS,
    ConfigurationState,
    SetupClient,
)
from blueferry.tel_links import dial_request, known_contact_name

# Bounds on the per-window avatar cache (least recently used entries are
# dropped). The count covers a long list of small photos; the byte bound
# keeps large ones (up to limits.MAX_CONTACT_PHOTO_BYTES each) from holding
# hundreds of MiB of encoded images. It matches the daemon's per-sync photo
# budget (limits.MAX_CONTACT_PHOTOS_TOTAL_BYTES), so every stored photo fits.
MAX_CACHED_AVATARS = 512
MAX_CACHED_AVATAR_BYTES = 16 * 1024 * 1024
MAX_PENDING_AVATARS = 32
AVATAR_RETRY_SECONDS = 30
AVATAR_RETRY_MAX_SECONDS = 600


class BridgeController(QObject):
    threadsChanged = Signal()
    contactResultsChanged = Signal()
    statusChanged = Signal()
    devicesChanged = Signal()
    bluetoothChanged = Signal()
    busyChanged = Signal()
    errorTextChanged = Signal()
    compatibilityChanged = Signal()
    configuredChanged = Signal()
    setupLoadedChanged = Signal()
    onboardingStageChanged = Signal()
    pairingConfirmationRequested = Signal(str)
    pairingIssueReportChanged = Signal()
    messageOpenRequested = Signal(str)
    notificationOpenMapChanged = Signal()
    groupConfirmationRequested = Signal(str, str, str)
    threadSendSucceeded = Signal(str, str)
    messageSendSucceeded = Signal(str, str)
    avatarsChanged = Signal()
    callHistoryChanged = Signal()
    nowPlayingChanged = Signal()
    notificationsChanged = Signal()
    photosChanged = Signal()
    companionToolsChanged = Signal()
    pluginSettingsChanged = Signal()
    pluginSurfacesChanged = Signal()
    phoneIdentityChanged = Signal()
    featuresChanged = Signal()
    doctorChanged = Signal()
    phoneCallsChanged = Signal()
    # (number, problem) of a clicked tel:/callto: link to confirm; see
    # tel_links.dial_request. Never dials by itself.
    dialLinkRequested = Signal(str, str)

    def __init__(
        self,
        backend: BackendClient | None = None,
        setup: SetupClient | None = None,
        *,
        subscribe: bool = True,
        autostart: bool = True,
        parent: QObject | None = None,
        companion: CompanionTools | None = None,
        plugin_settings: PluginSettings | None = None,
        plugin_surfaces: PluginSurfaces | None = None,
        doctor: Callable[[], DoctorReport] = run_doctor,
    ) -> None:
        super().__init__(parent)
        self._backend = backend or BackendClient()
        self._setup = setup or SetupClient()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        # dbus-python connections are thread-affine in practice. Keep the one
        # serialized worker alive rather than letting QThreadPool replace it.
        self._pool.setExpiryTimeout(-1)
        self._tasks: set[Task] = set()
        self._threads: list[dict] = []
        self._pending_group: tuple[str, str, str] | None = None
        self._contact_results: list[dict] = []
        self._state = ConversationState(select_first=False)
        self._status: dict = {}
        self._notification_open_map: list[dict] = []
        self._devices: list[dict] = []
        # Settings: local.env switches (Messages1.GetFeatures) and the doctor
        # report; the doctor runs a local program on its own pool so the
        # D-Bus worker stays free.
        self._features: dict = {"loaded": False, "available": False, "items": {},
                                "notice": "", "error": ""}
        self._doctor_runner = doctor
        self._doctor: dict = {"running": False, "ran": False, "ok": False,
                              "warnings": False, "text": ""}
        self._local_pool = QThreadPool(self)
        self._local_pool.setMaxThreadCount(1)
        self._bluetooth_active = False
        self._busy_count = 0
        self._error_text = ""
        self._reconnect_pending = False
        self._reconnect_notice = ""
        self._pairing_issue_report = ""
        self._compatibility: dict = {}
        self._configuration = ConfigurationState(False, "", "", "")
        self._setup_loaded = False
        self._onboarding = OnboardingState()
        self._onboarding_stage = str(self._onboarding.stage)
        self._refreshing = False
        self._refresh_again = False
        # Optional HFP calls (Calls1); empty unless the backend enables them.
        self._phone_calls: list[dict] = []
        self._calls_state = "disabled"
        # (calls_enabled, calls_state) of the last status that refreshed the
        # call list. Calls themselves are announced by CallsChanged.
        self._calls_status_key: tuple[object, object] | None = None
        self._storage_unlock_attempted = False
        self._pairing_confirmation_lock = threading.Lock()
        self._pairing_confirmation: tuple[threading.Event, list[bool]] | None = None
        # Opt-in avatars (status["contact_photos"]). Bytes are fetched on the
        # worker and read by the QML image provider's thread, hence the lock.
        self._avatar_lock = threading.Lock()
        # LRU caches: displayed avatars are touched on every binding read.
        self._avatars: OrderedDict[str, bytes] = OrderedDict()
        self._avatar_missing: OrderedDict[str, None] = OrderedDict()
        self._avatar_pending: set[str] = set()
        self._avatar_backoff: OrderedDict[str, None] = OrderedDict()
        self._avatar_failures: OrderedDict[str, int] = OrderedDict()
        # Addresses QML asked for since the last avatarsChanged: those are on
        # screen, so a new arrival never evicts them (that would blank and
        # refetch a shown avatar, and its arrival would evict the next one).
        self._avatar_pinned: set[str] = set()
        # Generation: changes only when the daemon's photo cache changes and
        # is part of every avatar URL. Revision: bumped on each arrival so
        # QML re-reads avatarSource(); it never changes a shown avatar's URL.
        self._avatar_generation = 0
        self._avatar_revision = 0
        self._photo_status: tuple[bool, object] = (False, None)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self._call_history: list[dict] = []
        # Day-grouped rows for the Calls tab ("all" and "missed").
        self._call_history_rows: dict = {}
        self._call_history_error = ""
        self._call_history_loading = False
        self._call_history_again = False
        # Only a visible Recent Calls page keeps call records in this process.
        self._call_history_watched = False
        self._call_history_timer = QTimer(self)
        self._call_history_timer.setSingleShot(True)
        self._call_history_timer.setInterval(100)
        self._call_history_timer.timeout.connect(self.loadCallHistory)
        # Opt-in iPhone media control: coalesce NowPlayingChanged bursts and
        # fetch the private snapshot through Media1 only when enabled.
        self._now_playing: dict = {}
        self._now_playing_timer = QTimer(self)
        self._now_playing_timer.setSingleShot(True)
        self._now_playing_timer.setInterval(150)
        self._now_playing_timer.timeout.connect(self.refreshNowPlaying)
        # Opt-in app-notification list: fetched only while its tab is shown.
        self._notifications: list[dict] = []
        self._notifications_info: dict = {"enabled": False, "content": False, "error": ""}
        self._notifications_watched = False
        self._notifications_timer = QTimer(self)
        self._notifications_timer.setSingleShot(True)
        self._notifications_timer.setInterval(150)
        self._notifications_timer.timeout.connect(self.refreshNotifications)
        self.devicesChanged.connect(self.phoneIdentityChanged)
        self.configuredChanged.connect(self.phoneIdentityChanged)
        # The Photos tab (a plugin with the photos capability).
        self._photos_watched = False
        self._photos_plugin: object = None
        self._photos: dict = {"present": False, "ready": False, "loaded": False,
                              "hint": "", "items": []}
        # UxPlay, LocalSend and iPhone photos: client-side, no daemon involved.
        self._companion = companion or CompanionTools(parent=self)
        self._companion.changed.connect(self.companionToolsChanged)
        # Plugin management runs git, pip and plugin calls on its own queue.
        self._plugin_settings = plugin_settings or PluginSettings(parent=self)
        self._plugin_settings.changed.connect(self.pluginSettingsChanged)
        self._bus = QDBusConnection.sessionBus() if subscribe else None
        # "From Plugins" and "Send to…", on their own pool.
        self._plugin_surfaces = plugin_surfaces or PluginSurfaces(
            bus=self._bus, parent=self,
        )
        self._plugin_surfaces.changed.connect(self.pluginSurfacesChanged)
        # Enabling, installing or removing a plugin changes the card.
        self._plugin_set: tuple = ()
        self._plugin_settings.changed.connect(self._plugin_set_changed)
        if subscribe:
            self._subscribe()
        if autostart:
            QTimer.singleShot(0, self.start)

    @Property("QVariantList", notify=threadsChanged)
    def threads(self):
        return self._threads

    @Property("QVariantList", notify=contactResultsChanged)
    def contactResults(self):
        return self._contact_results

    @Property("QVariantList", notify=phoneCallsChanged)
    def phoneCalls(self):
        return self._phone_calls

    @Property(str, notify=phoneCallsChanged)
    def callsState(self) -> str:
        return self._calls_state

    @Property("QVariantMap", notify=statusChanged)
    def status(self):
        return self._status

    @Property("QVariantMap", notify=statusChanged)
    def phoneAudio(self):
        """The PC/iPhone sound switch; QML wants camelCase ``onPc``."""
        audio = phone_audio_view.phone_audio(self._status)
        return {
            "supported": audio["supported"],
            "available": audio["available"],
            "onPc": audio["on_pc"],
            "pending": audio["pending"],
            "hint": audio["hint"],
            "playing": audio["playing"],
            "mismatch": audio["mismatch"],
        }

    @Property("QVariantMap", notify=statusChanged)
    def reconnect(self):
        """Manual reconnect: available, offered (button), attention, hint."""
        view = reconnect_view(self._status)
        return {
            "available": view.available,
            "offered": view.offered,
            "attention": view.attention,
            "hint": self._reconnect_notice or view.hint,
        }

    @Slot()
    def reconnectPhone(self) -> None:
        """Skip the automatic backoff and page the iPhone once now."""
        def completed(value: object) -> None:
            result = str(value)
            if result == "unreachable":
                self._reconnect_pending = False
                self._reconnect_notice = ""
                self._set_error(UNREACHABLE_TEXT)
            else:
                self._reconnect_pending = result in ("started", "profile-reset")
                self._reconnect_notice = result_text(result)
            self.statusChanged.emit()
            self.refresh()

        self._run(self._backend.reconnect_phone, completed, busy=False)

    def _follow_manual_reconnect(self) -> None:
        """Turn the status outcome of a manual attempt into a clear message."""
        if not self._reconnect_pending:
            self._reconnect_notice = ""
            return
        state = self._status.get("phone_reconnect_state")
        if state in ("connected", "unreachable", "waiting"):
            self._reconnect_pending = False
            self._reconnect_notice = ""
        if state == "unreachable":
            self._set_error(UNREACHABLE_TEXT)

    @Property(int, notify=avatarsChanged)
    def avatarRevision(self) -> int:
        return self._avatar_revision

    @Slot(str, result=str)
    def avatarSource(self, address: str) -> str:
        """Provider URL for a fetched avatar, else ``""`` (fetching lazily).

        The URL carries the cache *generation*, which changes only when the
        daemon reloads its contacts, so an avatar that is already shown keeps
        the same URL (and is not reloaded) while other avatars arrive.
        ``avatarRevision`` only tells QML to re-read this slot. Addresses with
        no photo are remembered until the generation changes; failed lookups
        (rate limit, busy store) are retried after a backoff.
        """
        key = str(address or "").strip()
        if not key or not self._status.get("contact_photos"):
            return ""
        if len(self._avatar_pinned) < MAX_CACHED_AVATARS:
            self._avatar_pinned.add(key)
        with self._avatar_lock:
            if key in self._avatars:
                self._avatars.move_to_end(key)
                return avatar_url(key, self._avatar_generation)
        if (
            key in self._avatar_missing
            or key in self._avatar_pending
            or key in self._avatar_backoff
            or len(self._avatar_pending) >= MAX_PENDING_AVATARS
        ):
            return ""
        self._avatar_pending.add(key)
        generation = self._avatar_generation

        def fetched(value: object) -> None:
            self._avatar_pending.discard(key)
            if generation != self._avatar_generation:
                self._notify_avatar_slot_released()
                return
            self._avatar_failures.pop(key, None)
            data = value if isinstance(value, bytes) and value else None
            if data is None:
                self._avatar_missing[key] = None
                while len(self._avatar_missing) > MAX_CACHED_AVATARS:
                    self._avatar_missing.popitem(last=False)
                self._notify_avatar_slot_released()
                return
            if not self._cache_avatar(key, data):
                # Everything else in the cache is on screen: keep the shown
                # avatars and try this one again later instead of evicting
                # one, which would only be refetched at once.
                self._avatar_backoff[key] = None
                while len(self._avatar_backoff) > MAX_CACHED_AVATARS:
                    self._avatar_backoff.popitem(last=False)
                self._schedule_avatar_retry(AVATAR_RETRY_SECONDS * 1000, key, generation)
                self._notify_avatar_slot_released()
                return
            self._avatars_changed()

        def failed(_message: str) -> None:
            # Avatars are decoration: never surface a lookup, busy-store, or
            # rate-limit failure as an error banner. Retry after a backoff.
            self._avatar_pending.discard(key)
            if generation != self._avatar_generation:
                self._notify_avatar_slot_released()
                return
            attempts = self._avatar_failures.pop(key, 0) + 1
            self._avatar_failures[key] = attempts
            self._avatar_backoff[key] = None
            # Bounded like the other caches. Evicting a backoff entry only
            # allows an earlier retry, which the daemon's rate limit covers.
            for pending in (self._avatar_failures, self._avatar_backoff):
                while len(pending) > MAX_CACHED_AVATARS:
                    pending.popitem(last=False)
            delay = min(
                AVATAR_RETRY_SECONDS * 2 ** (attempts - 1), AVATAR_RETRY_MAX_SECONDS,
            )
            self._schedule_avatar_retry(int(delay * 1000), key, generation)
            self._notify_avatar_slot_released()

        self._run(lambda: self._backend.contact_photo(key), fetched, failed, busy=False)
        return ""

    def _notify_avatar_slot_released(self) -> None:
        # QML may have tried other visible addresses while every fetch slot
        # was occupied. Empty, stale, and failed replies free a slot too.
        self._avatars_changed()

    def _cache_avatar(self, key: str, data: bytes) -> bool:
        """Store an arrival within the bounds; ``False`` if it did not fit.

        Least recently used entries go first, but never one that is on
        screen (pinned). A single avatar above the byte bound is still kept
        when nothing else is cached.
        """
        with self._avatar_lock:
            self._avatars[key] = data
            cached = sum(len(item) for item in self._avatars.values())

            def over() -> bool:
                return (
                    len(self._avatars) > MAX_CACHED_AVATARS
                    or cached > MAX_CACHED_AVATAR_BYTES
                )

            for old in list(self._avatars):
                if not over() or len(self._avatars) == 1:
                    break
                if old == key or old in self._avatar_pinned:
                    continue
                cached -= len(self._avatars.pop(old))
            if over() and len(self._avatars) > 1:
                del self._avatars[key]
                return False
        return True

    def _avatars_changed(self) -> None:
        """Tell QML to re-read avatarSource(); its reads pin what is shown."""
        self._avatar_pinned.clear()
        self._avatar_revision += 1
        self.avatarsChanged.emit()

    def _schedule_avatar_retry(self, delay_ms: int, key: str, generation: int) -> None:
        def release() -> None:
            if generation != self._avatar_generation:
                return
            self._avatar_backoff.pop(key, None)
            # Let QML ask again; the binding re-reads avatarSource().
            self._avatars_changed()

        QTimer.singleShot(delay_ms, self, release)

    def avatar_bytes(self, address: str) -> bytes | None:
        """Thread-safe lookup used by :class:`AvatarImageProvider`."""
        with self._avatar_lock:
            return self._avatars.get(address)

    def _sync_avatars(self) -> None:
        """Drop cached avatars when photos toggle or the contact cache reloads."""
        current = (
            bool(self._status.get("contact_photos")),
            self._status.get("contact_photo_revision"),
        )
        if current == self._photo_status:
            return
        self._photo_status = current
        with self._avatar_lock:
            self._avatars.clear()
        self._avatar_missing.clear()
        self._avatar_backoff.clear()
        self._avatar_failures.clear()
        self._avatar_generation += 1
        self._avatars_changed()

    @Property(bool, notify=statusChanged)
    def callHistoryEnabled(self) -> bool:
        return self._status.get("call_history_enabled") is True

    @Property("QVariantList", notify=callHistoryChanged)
    def callHistory(self):
        return self._call_history

    @Property("QVariantMap", notify=callHistoryChanged)
    def callHistoryRows(self):
        """Day-grouped rows for the Calls tab: ``all`` and ``missed``."""
        return self._call_history_rows

    @Property("QVariantMap", notify=statusChanged)
    def featureHints(self):
        return phone_link.feature_hints(self._status)

    @Property(str, notify=phoneIdentityChanged)
    def phoneName(self) -> str:
        mac = self._configuration.mac
        for device in self._devices:
            if mac and device.get("mac") == mac and device.get("name"):
                return str(device["name"])
        return "iPhone"

    @Property("QVariantMap", notify=pluginSettingsChanged)
    def pluginSettings(self):
        return self._plugin_settings.state()

    @Slot()
    def loadPlugins(self) -> None:
        self._plugin_settings.load()

    @Slot(str, bool)
    def setPluginEnabled(self, plugin_id: str, enabled: bool) -> None:
        self._plugin_settings.set_enabled(str(plugin_id), bool(enabled))

    @Slot(str, str)
    def preparePluginInstall(self, url: str, ref: str) -> None:
        self._plugin_settings.prepare_install(str(url), str(ref))

    @Slot(str)
    def installStorePlugin(self, plugin_id: str) -> None:
        self._plugin_settings.install_from_store(str(plugin_id))

    @Slot(str)
    def preparePluginUpdate(self, plugin_id: str) -> None:
        self._plugin_settings.prepare_update(str(plugin_id))

    @Slot()
    def confirmPluginInstall(self) -> None:
        self._plugin_settings.confirm_install()

    @Slot()
    def cancelPluginInstall(self) -> None:
        self._plugin_settings.cancel_install()

    @Slot(str)
    def removePlugin(self, plugin_id: str) -> None:
        self._plugin_settings.remove(str(plugin_id))

    @Slot(str)
    def loadPluginConfig(self, plugin_id: str) -> None:
        self._plugin_settings.load_config(str(plugin_id))

    @Slot(str, "QVariantMap")
    def savePluginConfig(self, plugin_id: str, values: dict) -> None:
        self._plugin_settings.save_config(str(plugin_id), dict(values or {}))

    @Slot()
    def closePluginConfig(self) -> None:
        self._plugin_settings.close_config()

    @Slot(str, "QVariantMap", result="QVariantMap")
    def checkPluginConfig(self, plugin_id: str, values: dict) -> dict:
        return self._plugin_settings.check_config(str(plugin_id), dict(values or {}))

    @Slot(str, "QVariantMap")
    def testPluginConfig(self, plugin_id: str, values: dict) -> None:
        self._plugin_settings.test_config(str(plugin_id), dict(values or {}))

    @Slot(str, "QVariantMap")
    def signInPlugin(self, plugin_id: str, values: dict) -> None:
        self._plugin_settings.sign_in(str(plugin_id), dict(values or {}))

    @Slot()
    def cancelPluginSignIn(self) -> None:
        self._plugin_settings.cancel_sign_in()

    @Slot(str)
    def openPluginLog(self, plugin_id: str) -> None:
        self._plugin_settings.open_log(str(plugin_id))

    @Slot(str, str)
    def openPluginHelp(self, plugin_id: str, key: str) -> None:
        self._plugin_settings.open_help(str(plugin_id), str(key))

    @Slot(str, str)
    def runPluginConfigAction(self, plugin_id: str, action_id: str) -> None:
        self._plugin_settings.run_action(str(plugin_id), str(action_id))

    @Slot(bool)
    def loadPluginStore(self, refresh: bool) -> None:
        self._plugin_settings.load_store(bool(refresh))

    @Slot("QVariantList")
    def setPluginIndexes(self, urls: list) -> None:
        self._plugin_settings.set_indexes([str(url) for url in urls or []])

    @Slot()
    def clearPluginMessage(self) -> None:
        self._plugin_settings.clear_message()

    def _plugin_set_changed(self) -> None:
        current = tuple(
            (row.get("id"), row.get("enabled"))
            for row in self._plugin_settings.state().get("plugins", [])
        )
        if current != self._plugin_set:
            known = bool(self._plugin_set)
            self._plugin_set = current
            if known:
                self._plugin_surfaces.schedule_reload()
                # The photo plugin may be gone or disabled now: forget it
                # until the next load finds it again.
                self._photos_plugin = None
                self.refreshPhotos()

    @Property("QVariantMap", notify=pluginSurfacesChanged)
    def pluginSurfaces(self):
        """cards (per plugin: name, ok, hint, items with actions), busy,
        message, targets for "Send to…"; everything plain text."""
        return self._plugin_surfaces.state()

    @Slot()
    def refreshPluginCards(self) -> None:
        self._plugin_surfaces.reload()

    @Slot(str, str, str)
    def invokePluginAction(self, plugin_id: str, item_id: str, action_id: str) -> None:
        self._plugin_surfaces.invoke(str(plugin_id), str(item_id), str(action_id))

    @Slot()
    def clearPluginCardMessage(self) -> None:
        self._plugin_surfaces.clear_message()

    @Slot()
    def loadShareTargets(self) -> None:
        self._plugin_surfaces.load_targets()

    @Slot(str, "QVariantList")
    def sendToTarget(self, key: str, urls: list) -> None:
        self._plugin_surfaces.send(str(key), list(urls or []))

    @Slot(str, str, str, "QVariantList")
    def sendFromPluginCard(self, plugin_id: str, target_id: str, label: str, urls: list) -> None:
        """A card action with send_to, or files dropped on a card item."""
        self._plugin_surfaces.send_from_card(
            str(plugin_id), str(target_id), str(label), list(urls or []))

    @Property("QVariantMap", notify=companionToolsChanged)
    def companionTools(self):
        return self._companion.state()

    @Slot()
    def refreshCompanionTools(self) -> None:
        self._companion.refresh()

    @Slot(str)
    def runCompanionTool(self, action: str) -> None:
        self._companion.run(action)

    @Slot()
    def clearCompanionMessage(self) -> None:
        self._companion.clear_message()

    @Property("QVariantMap", notify=photosChanged)
    def photos(self):
        """present, ready, loaded, hint and items (id, label, video, thumbnail, original)."""
        return self._photos

    @Slot(bool)
    def watchPhotos(self, watched: bool) -> None:
        """The Photos tab became visible (load) or hidden (forget the list)."""
        self._photos_watched = bool(watched)
        if self._photos_watched:
            self.refreshPhotos()
            return
        if self._photos["items"]:
            self._photos = {**self._photos, "items": []}
            self.photosChanged.emit()

    @Slot()
    def refreshPhotos(self) -> None:
        if not self._photos_watched:
            return

        def load():
            manifest = photos_view.find_plugin()
            return manifest, photos_view.load_recent(manifest)

        def completed(value: object) -> None:
            manifest, snapshot = value  # type: ignore[misc]
            self._photos_plugin = manifest
            if not self._photos_watched:
                return
            self._photos = {
                "present": snapshot.present,
                "ready": snapshot.ready,
                "loaded": True,
                "hint": snapshot.hint,
                "items": [_photo_item(photo) for photo in snapshot.photos],
            }
            self.photosChanged.emit()

        def failed(message: str) -> None:
            # Never leave the tab spinning: show why instead.
            self._photos = {
                "present": self._photos_plugin is not None, "ready": False, "loaded": True,
                "hint": message or _("Photos unavailable"), "items": [],
            }
            self.photosChanged.emit()

        self._run(load, completed, failed, busy=False)

    @Slot(str)
    def openPhoto(self, photo_id: str) -> None:
        """Download the original (plugin) and open it in the default viewer."""
        manifest = self._photos_plugin
        if manifest is None:
            return

        def fetch() -> object:
            # Disabled or removed since the list loaded: never call it (a call
            # would start a disabled plugin again by D-Bus activation).
            current = photos_view.find_plugin()
            if current is None or current.id != manifest.id:  # type: ignore[attr-defined]
                return None
            return photos_view.fetch_original(current, str(photo_id))

        def completed(value: object) -> None:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            if value is None:
                return
            url = QUrl.fromLocalFile(str(value))
            items = [
                {**item, "original": url.toString()} if item["id"] == photo_id else item
                for item in self._photos["items"]
            ]
            self._photos = {**self._photos, "items": items}
            self.photosChanged.emit()
            QDesktopServices.openUrl(url)

        self._run(fetch, completed)

    @Property("QVariantList", notify=notificationsChanged)
    def notifications(self):
        return self._notifications

    @Property("QVariantMap", notify=notificationsChanged)
    def notificationsInfo(self):
        return self._notifications_info

    @Slot(bool)
    def watchNotifications(self, watched: bool) -> None:
        """The Notifications tab became visible (load) or hidden (forget)."""
        self._notifications_watched = bool(watched)
        if self._notifications_watched:
            self.refreshNotifications()
            return
        self._notifications_timer.stop()
        if self._notifications:
            self._notifications = []
            self.notificationsChanged.emit()

    @Slot()
    def refreshNotifications(self) -> None:
        if not self._notifications_watched:
            return

        def completed(value: object) -> None:
            if not self._notifications_watched:
                return
            snapshot = value if isinstance(value, dict) else {}
            self._notifications = phone_link.notification_rows(snapshot.get("notifications"))
            self._notifications_info = {
                "enabled": snapshot.get("enabled") is True,
                "content": snapshot.get("content") is True,
                "error": "",
            }
            self.notificationsChanged.emit()

        def failed(message: str) -> None:
            self._notifications = []
            self._notifications_info = {
                **self._notifications_info, "error": message or _("Notifications are unavailable"),
            }
            self.notificationsChanged.emit()

        self._run(lambda: self._backend.notifications(100), completed, failed, busy=False)

    @Slot()
    def _notificationsInvalidated(self) -> None:
        if self._notifications_watched:
            self._notifications_timer.start()

    @Property(str, notify=callHistoryChanged)
    def callHistoryError(self) -> str:
        return self._call_history_error
    @Property("QVariantList", notify=notificationOpenMapChanged)
    def notificationOpenMap(self):
        return self._notification_open_map

    @Property("QVariantMap", notify=nowPlayingChanged)
    def nowPlaying(self):
        return self._now_playing

    @Property("QVariantList", notify=devicesChanged)
    def devices(self):
        return self._devices

    @Property(bool, notify=bluetoothChanged)
    def bluetoothActive(self) -> bool:
        return self._bluetooth_active

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy_count > 0

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(str, notify=pairingIssueReportChanged)
    def pairingIssueReport(self) -> str:
        return self._pairing_issue_report

    @Property("QVariantMap", notify=compatibilityChanged)
    def compatibility(self):
        return self._compatibility

    @Property(bool, notify=compatibilityChanged)
    def compatibilityLoaded(self) -> bool:
        return bool(self._compatibility)

    def _set_compatibility_unavailable(self, adapter: str | None, message: str) -> None:
        self._compatibility = {
            "adapter": adapter or "",
            "available": False,
            "hardware_supported": False,
            "messages_supported": False,
            "notifications_supported": False,
            "notifications_active": False,
            "bearer_api_active": False,
            "pairing_ready": True,
            "issue": message,
            "adapters": [],
        }
        self.compatibilityChanged.emit()
        self._update_onboarding_stage()
        self._operation_failed(message)

    def _onboarding_compatibility(self) -> dict:
        return effective_compatibility(
            self._compatibility,
            self._configuration,
        )

    @Property("QVariantMap", notify=compatibilityChanged)
    def onboardingCompatibility(self):
        return self._onboarding_compatibility()

    @Property(bool, notify=configuredChanged)
    def configured(self) -> bool:
        return self._configuration.configured

    @Property(bool, notify=configuredChanged)
    def targetSaved(self) -> bool:
        return self._configuration.saved

    @Property(str, notify=configuredChanged)
    def configuredMac(self) -> str:
        return self._configuration.mac

    @Property(bool, notify=setupLoadedChanged)
    def setupLoaded(self) -> bool:
        return self._setup_loaded

    @Property(str, notify=onboardingStageChanged)
    def onboardingStage(self) -> str:
        return self._onboarding_stage

    def _update_onboarding_stage(self) -> None:
        transition = self._onboarding.update(
            setup_loaded=self._setup_loaded,
            compatibility=self._compatibility,
            configuration=self._configuration,
            status=BackendStatus.from_dict(self._status),
        )
        stage = str(transition.current)
        if stage == self._onboarding_stage:
            return
        self._onboarding_stage = stage
        self.onboardingStageChanged.emit()

    @Property(str, constant=True)
    def version(self) -> str:
        return __version__

    @Property(str, constant=True)
    def bluetoothRestartCommand(self) -> str:
        return bluetooth_restart_command() or ""

    def _set_error(self, message: str) -> None:
        if message == self._error_text:
            return
        self._error_text = message
        self.errorTextChanged.emit()

    def _set_pairing_issue_report(self, path: str) -> None:
        value = str(path or "").strip()
        if value == self._pairing_issue_report:
            return
        self._pairing_issue_report = value
        self.pairingIssueReportChanged.emit()

    def _refresh_pairing_issue_report(self) -> None:
        found = issue_report()
        self._set_pairing_issue_report(str(found) if found is not None else "")

    @Slot()
    def filePairingIssue(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(issue_url(self._pairing_issue_report or None)))

    def _set_busy(self, delta: int) -> None:
        was_busy = self.busy
        self._busy_count = max(0, self._busy_count + delta)
        if self.busy != was_busy:
            self.busyChanged.emit()

    def _run(
        self,
        operation: Callable[[], object],
        on_done: Callable[[object], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
        *,
        busy: bool = True,
    ) -> None:
        task = Task(operation)
        self._tasks.add(task)
        if busy:
            self._set_busy(1)
        if on_done is not None:
            task.signals.done.connect(on_done)
        task.signals.failed.connect(on_failed or self._operation_failed)

        def finished() -> None:
            self._tasks.discard(task)
            if busy:
                self._set_busy(-1)

        task.signals.finished.connect(finished)
        self._pool.start(task)

    def _operation_failed(self, message: str) -> None:
        self._set_error(message or _("Operation failed"))

    def _subscribe(self) -> None:
        if self._bus is None:
            return
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "HistoryChanged",
            self,
            SLOT("_historyChanged(QVariantMap)"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "StatusChanged",
            self,
            SLOT("_statusInvalidated()"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "OpenMessageRequested",
            self,
            SLOT("_openMessageRequested(QString)"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "NotificationsChanged",
            self,
            SLOT("_notificationsInvalidated()"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "CallHistoryChanged",
            self,
            SLOT("_callHistoryInvalidated()"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "NowPlayingChanged",
            self,
            SLOT("_nowPlayingInvalidated()"),
        )
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            EVENTS_IFACE,
            "CallsChanged",
            self,
            SLOT("_callsInvalidated()"),
        )

    @Slot()
    def _nowPlayingInvalidated(self) -> None:
        self._now_playing_timer.start()

    def _set_now_playing(self, value: object) -> None:
        snapshot = dict(value) if isinstance(value, dict) else {}
        if snapshot != self._now_playing:
            self._now_playing = snapshot
            self.nowPlayingChanged.emit()

    @Slot()
    def refreshNowPlaying(self) -> None:
        if not self._status.get("media_control_enabled"):
            self._set_now_playing({})
            return
        self._run(
            self._backend.now_playing,
            self._set_now_playing,
            lambda _message: self._set_now_playing({}),
            busy=False,
        )

    @Slot(str)
    def sendMediaCommand(self, command: str) -> None:
        selected = str(command or "").strip()
        if not selected:
            return
        self._run(
            lambda: self._backend.send_media_command(selected),
            lambda _value: None,
            busy=False,
        )

    @Slot(str)
    def setPhoneAudioRoute(self, route: str) -> None:
        """Explicit user action: move the iPhone's sound here or back."""
        selected = str(route or "")
        if selected not in ("pc", "phone"):
            return

        def done(_value: object) -> None:
            self._refresh_timer.start()

        self._run(
            lambda: self._backend.set_phone_audio_route(selected), done, busy=False,
        )

    @Slot("QVariantMap")
    def _historyChanged(self, _revision) -> None:
        self._refresh_timer.start()
        # ClearHistory also empties the notification list.
        self._notificationsInvalidated()

    @Slot()
    def _statusInvalidated(self) -> None:
        self._refresh_timer.start()

    @Slot(str)
    def _openMessageRequested(self, handle: str) -> None:
        self.messageOpenRequested.emit(handle)

    @Slot()
    def _callHistoryInvalidated(self) -> None:
        # The signal carries nothing; only a shown list is refetched.
        if self.callHistoryEnabled and self._call_history_watched:
            self._call_history_timer.start()

    @Slot(bool)
    def watchCallHistory(self, watched: bool) -> None:
        """The Recent Calls page opened (load now) or closed (forget it)."""
        self._call_history_watched = bool(watched)
        if self._call_history_watched:
            self.loadCallHistory()
            return
        self._call_history_timer.stop()
        self._call_history_again = False
        if self._call_history or self._call_history_error:
            self._call_history = []
            self._call_history_rows = {}
            self._call_history_error = ""
            self.callHistoryChanged.emit()

    @Slot()
    def loadCallHistory(self) -> None:
        """Fetch the opt-in call list; never touches the conversation error."""
        if not self.callHistoryEnabled or not self._call_history_watched:
            return
        if self._call_history_loading:
            self._call_history_again = True
            return
        self._call_history_loading = True

        def operation() -> tuple[list[dict], dict]:
            entries = self._backend.call_history(200)
            rows = {
                "all": phone_overview.call_groups(entries),
                "missed": phone_overview.call_groups(entries, missed_only=True),
            }
            return [entry.to_dict() for entry in entries], rows

        def completed(value: object) -> None:
            # A reply that lands after the page closed is discarded.
            if self._call_history_watched:
                calls, rows = value if isinstance(value, tuple) else ([], {})
                self._call_history = list(calls)
                self._call_history_rows = dict(rows)
                self._call_history_error = ""
                self.callHistoryChanged.emit()
            finished()

        def failed(message: str) -> None:
            if self._call_history_watched:
                self._call_history_error = message or _("Call history is unavailable")
                self.callHistoryChanged.emit()
            finished()

        def finished() -> None:
            self._call_history_loading = False
            if self._call_history_again:
                self._call_history_again = False
                self.loadCallHistory()

        self._run(operation, completed, failed, busy=False)

    @Slot()
    def syncCallHistory(self) -> None:
        if not self.callHistoryEnabled:
            return

        def failed(message: str) -> None:
            if self._call_history_watched:
                self._call_history_error = message or _("Call history sync failed")
                self.callHistoryChanged.emit()

        self._run(
            self._backend.sync_call_history,
            lambda _value: self.loadCallHistory(),
            failed,
        )

    @Slot()
    def _callsInvalidated(self) -> None:
        self.refreshCalls()

    @Slot()
    def start(self) -> None:
        def initialize():
            configuration = self._setup.configuration()
            status = ensure_backend_current() if configuration.configured else {}
            return configuration, status

        def ready(result: object) -> None:
            configuration, status = result
            self._configuration = configuration
            self._setup_loaded = True
            self.configuredChanged.emit()
            self.compatibilityChanged.emit()
            self.setupLoadedChanged.emit()
            if status:
                self._status = dict(status)
                self._state.status = BackendStatus.from_dict(self._status)
                self.statusChanged.emit()
                self._sync_avatars()
                self._maybe_unlock_storage()
            self._set_error("")
            if self._configuration.configured:
                self.refresh()
            self.loadSetupState()
            self.loadDevices(False)
            self._update_onboarding_stage()

        def failed(message: str) -> None:
            self._set_error(_("Background service unavailable: {error}").format(error=message))
            self._setup_loaded = True
            self.setupLoadedChanged.emit()
            self._update_onboarding_stage()
            self.loadSetupState()
            self.loadDevices(False)

        self._run(initialize, ready, failed)

    @Slot()
    def loadSetupState(self) -> None:
        self._reload_setup_state(scan_after=False)

    def _reload_setup_state(self, *, scan_after: bool = False) -> None:
        selected = str(self._compatibility.get("adapter", "")).strip() or None
        self._compatibility = {}
        self.compatibilityChanged.emit()

        def operation():
            return self._setup.compatibility(selected), self._setup.configuration()

        def completed(value: object) -> None:
            compatibility, configuration = value
            self._compatibility = compatibility.to_dict()
            self._configuration = configuration
            self._setup_loaded = True
            self._bluetooth_active = compatibility.notifications_active
            self.compatibilityChanged.emit()
            self.configuredChanged.emit()
            self.setupLoadedChanged.emit()
            self.bluetoothChanged.emit()
            self._update_onboarding_stage()
            self._set_pairing_issue_report(configuration.pairing_issue_report)
            if scan_after:
                self.loadDevices(True)
            elif self._devices:
                self.loadDevices(False)

        def failed(message: str) -> None:
            self._set_compatibility_unavailable(selected, message)
            if scan_after:
                self.loadDevices(True)

        self._run(operation, completed, failed, busy=False)

    @Slot(str)
    def selectAdapter(self, name: str) -> None:
        selected = name.strip()
        if not selected or selected == str(self._compatibility.get("adapter", "")):
            return
        if self._devices:
            self._devices = []
            self.devicesChanged.emit()
        self._compatibility = {}
        self.compatibilityChanged.emit()

        def completed(value: object) -> None:
            compatibility = value
            self._compatibility = compatibility.to_dict()
            self._bluetooth_active = bool(
                getattr(compatibility, "notifications_active", False)
            )
            self.compatibilityChanged.emit()
            self.bluetoothChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)

        def failed(message: str) -> None:
            self._set_compatibility_unavailable(selected, message)
            self.loadDevices(False)

        self._run(lambda: self._setup.compatibility(selected), completed, failed)

    def _snapshot(self) -> tuple[ConversationSnapshot, list[dict] | None]:
        snapshot = fetch_conversation_snapshot(self._backend)
        # Prepare QML's copy on the worker; keep typed models for shared decisions.
        threads = (
            [thread.to_dict() for thread in snapshot.threads]
            if snapshot.threads is not None else None
        )
        return snapshot, threads

    def _apply_snapshot(self, result: tuple[ConversationSnapshot, list[dict] | None]) -> None:
        snapshot, threads = result
        self._state.apply_snapshot(snapshot)
        if threads is not None:
            self._threads = threads
            self.threadsChanged.emit()
        if snapshot.status is not None or snapshot.status_error:
            self._status = self._state.status.to_dict()
            self._follow_manual_reconnect()
            self.statusChanged.emit()
            self._sync_avatars()
            self._maybe_unlock_storage()
            self._now_playing_timer.start()
            # StatusChanged also fires for unrelated changes (bearers,
            # storage, phone status); only a call-state change needs a new
            # ListCalls. Call list changes arrive through CallsChanged.
            calls_key = (self._status.get("calls_enabled"), self._status.get("calls_state"))
            if calls_key != self._calls_status_key:
                self._calls_status_key = calls_key
                if self._status.get("calls_enabled") or self._phone_calls:
                    self.refreshCalls()
        self._update_onboarding_stage()
        self._refresh_pairing_issue_report()
        self._set_error(self._state.error)

    @Slot()
    def refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True

        def finished() -> None:
            self._refreshing = False
            if self._refresh_again:
                self._refresh_again = False
                self.refresh()

        task = Task(self._snapshot)
        self._tasks.add(task)
        task.signals.done.connect(self._apply_snapshot)
        task.signals.failed.connect(self._set_error)
        task.signals.finished.connect(lambda: (self._tasks.discard(task), finished()))
        self._pool.start(task)

    @Slot(str, str, bool)
    def sendThread(self, key: str, body: str, confirm_group: bool) -> None:
        draft = body
        state = self._state
        pending = self._pending_group
        self._pending_group = None
        if confirm_group:
            if pending is None or pending[:2] != (key, body.strip()):
                self._set_error(ReplyDisposition.STALE_GROUP.message)
                return
        plan = state.plan_reply(
            body, thread_key=key, confirm_group=confirm_group,
            expected_group_token=pending[2] if confirm_group else None,
        )
        thread = plan.thread
        if plan.disposition is ReplyDisposition.CONFIRM_GROUP and thread is not None:
            self._pending_group = (key, plan.body, plan.expected_group_token)
            self.groupConfirmationRequested.emit(key, draft, "\n".join(thread.recipients))
            return
        if not plan.ready:
            if plan.disposition.message:
                self._set_error(plan.disposition.message)
            return

        def completed(_value: object) -> None:
            state.reply_sent(plan, preserve_selection=True)
            self.threadSendSucceeded.emit(key, draft)
            self.refresh()

        self._run(
            lambda: self._backend.send_to_thread(
                key, plan.body, confirm_group=plan.confirm_group,
                expected_group_token=plan.expected_group_token,
            ),
            completed,
        )

    @Slot(str, "QVariantList")
    def setGroupParticipants(self, key: str, recipients) -> None:
        selected = [str(value).strip() for value in recipients if str(value).strip()]
        if not key or not selected:
            return
        self._run(
            lambda: self._backend.set_group_participants(key, selected),
            lambda _value: self.refresh(),
        )

    @Slot(str)
    def findContacts(self, query: str) -> None:
        request = self._state.begin_contact_search(query)
        self._contact_results = []
        self.contactResultsChanged.emit()
        if request is None:
            return

        def completed(value: object) -> None:
            matches = list(value) if isinstance(value, list) else []
            if not self._state.apply_contact_results(request, matches):
                return
            self._contact_results = [
                {"name": str(name), "address": str(address)}
                for name, address in self._state.contact_results
            ]
            self.contactResultsChanged.emit()

        def failed(message: str) -> None:
            if self._state.apply_contact_results(request, []):
                self._set_error(message)

        self._run(
            lambda: self._backend.find_contacts(request.query),
            completed, failed, busy=False,
        )

    @Slot(str, str)
    def sendMessage(self, recipient: str, body: str) -> None:
        if not recipient.strip() or not body.strip():
            return

        def completed(_value: object) -> None:
            self.messageSendSucceeded.emit(recipient, body)
            self.refresh()

        self._run(
            lambda: self._backend.send(recipient.strip(), body.strip()),
            completed,
        )

    # ---- optional phone calls (Calls1) -----------------------------------

    def _apply_calls(self, snapshot: object) -> None:
        if not isinstance(snapshot, CallsSnapshot):
            return
        self._phone_calls = [call.to_dict() for call in snapshot.calls]
        self._calls_state = snapshot.state
        self.phoneCallsChanged.emit()

    def _calls_unavailable(self, _message: str = "") -> None:
        # Status explains a disabled or missing feature; an in-flight list
        # must not leave stale calls or a stale "ready" state on screen.
        state = str(self._status.get("calls_state") or "disabled")
        if state == "ready":
            state = "unavailable"  # ListCalls just failed despite the status
        if self._phone_calls or state != self._calls_state:
            self._phone_calls = []
            self._calls_state = state
            self.phoneCallsChanged.emit()

    @Slot()
    def refreshCalls(self) -> None:
        if not self._status.get("calls_enabled"):
            self._calls_unavailable()
            return
        self._run(self._backend.calls, self._apply_calls, self._calls_unavailable, busy=False)

    def _call_action(self, operation: Callable[[], object]) -> None:
        self._run(operation, lambda _value: self.refreshCalls())

    @Slot(str)
    def dialCall(self, number: str) -> None:
        selected = str(number or "").strip()
        if selected:
            self._call_action(lambda: self._backend.dial(selected))

    @Slot(str)
    def requestDialLink(self, link: str) -> None:
        """Ask QML to confirm a phone link; dialing waits for the user."""
        number, problem = dial_request(link)
        self.dialLinkRequested.emit(number, problem)

    @Slot(str, result=str)
    def contactNameForNumber(self, number: str) -> str:
        return known_contact_name(self._state, str(number or ""))

    @Slot(str)
    def answerCall(self, call_id: str) -> None:
        if call_id:
            self._call_action(lambda: self._backend.answer_call(str(call_id)))

    @Slot(str)
    def hangupCall(self, call_id: str) -> None:
        if call_id:
            self._call_action(lambda: self._backend.hangup_call(str(call_id)))

    @Slot()
    def hangupAllCalls(self) -> None:
        self._call_action(self._backend.hangup_all_calls)

    @Slot()
    def swapCalls(self) -> None:
        """Hold the active call (or resume the held one): HFP AT+CHLD=2."""
        self._call_action(self._backend.swap_calls)

    @Slot()
    def syncContacts(self) -> None:
        self._run(self._backend.sync_contacts, lambda _value: self.refresh())

    @Slot()
    def restartBackend(self) -> None:
        def restarted(_value: object) -> None:
            self._features = {**self._features, "notice": ""}
            QTimer.singleShot(800, self.refresh)
            QTimer.singleShot(1200, self.loadFeatures)

        self._run(restart_backend, restarted)

    # ---- settings: feature switches and the doctor report ------------------

    @Property("QVariantMap", notify=featuresChanged)
    def features(self):
        return self._features

    @Slot()
    def loadFeatures(self) -> None:
        def done(value: object) -> None:
            items = value if isinstance(value, dict) else {}
            self._features = {**self._features, "loaded": True, "available": True,
                              "items": items, "error": ""}
            self.featuresChanged.emit()

        def failed(message: str) -> None:
            # Older daemons have no GetFeatures; the UI falls back to hints.
            self._features = {**self._features, "loaded": True, "available": False,
                              "items": {}, "error": message}
            self.featuresChanged.emit()

        self._run(lambda: self._backend.features(), done, failed, busy=False)

    @Slot(str, bool)
    def setFeature(self, name: str, enabled: bool) -> None:
        def done(value: object) -> None:
            notice = {
                "restart-required": _("Restart the BlueFerry service to apply the change."),
                "environment": _(
                    "Saved, but the service's environment sets this variable and wins."
                ),
            }.get(str(value), "")
            self._features = {**self._features, "notice": notice}
            self.featuresChanged.emit()
            self.loadFeatures()

        self._run(lambda: self._backend.set_feature(str(name), bool(enabled)), done)

    @Property("QVariantMap", notify=doctorChanged)
    def doctor(self):
        return self._doctor

    @Slot()
    def runDoctor(self) -> None:
        if self._doctor["running"]:
            return
        self._doctor = {**self._doctor, "running": True}
        self.doctorChanged.emit()
        task = Task(self._doctor_runner)
        self._tasks.add(task)

        def done(report: object) -> None:
            if isinstance(report, DoctorReport):
                self._doctor = {"running": False, "ran": True, "ok": report.ok,
                                "warnings": report.warnings, "text": report.text}
            self.doctorChanged.emit()

        def failed(message: str) -> None:
            self._doctor = {"running": False, "ran": True, "ok": False,
                            "warnings": False, "text": message}
            self.doctorChanged.emit()

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        task.signals.finished.connect(lambda: self._tasks.discard(task))
        self._local_pool.start(task)

    @Slot()
    def clearHistory(self) -> None:
        self._run(
            self._backend.clear_history,
            lambda _value: self.refresh(),
        )

    @Slot(str, bool)
    def setThreadStarred(self, thread_key: str, starred: bool) -> None:
        key = str(thread_key or "").strip()
        if not key:
            return
        self._run(
            lambda: self._backend.set_thread_starred(key, bool(starred)),
            lambda _value: self.refresh(),
            busy=False,
        )

    @Slot(str)
    def markThreadRead(self, thread_key: str) -> None:
        key = str(thread_key or "").strip()
        if not key:
            return
        self._run(
            lambda: self._backend.mark_thread_read(key),
            lambda _value: None,
            lambda _message: None,
            busy=False,
        )

    @Slot("QVariantList")
    def deleteThreads(self, thread_keys) -> None:
        selected = [str(value) for value in thread_keys]
        if not selected:
            return
        self._run(
            lambda: self._backend.delete_threads(selected),
            lambda _value: self.refresh(),
        )

    @Slot(str)
    def setNotificationPolicy(self, policy: str) -> None:
        def completed(value: object) -> None:
            self._status["notification_policy"] = str(value)
            self.statusChanged.emit()

        self._run(lambda: self._backend.set_notification_policy(policy), completed)

    @Slot(bool)
    def setContactsOnlyNotifications(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            self._status["contacts_only_notifications"] = bool(value)
            self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_contacts_only_notifications(enabled),
            completed,
        )

    @Slot(bool)
    def setAncsNotificationActions(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            self._status["ancs_actions_preference"] = bool(value)
            self._status["ancs_actions"] = bool(value) and (
                self._status.get("notification_content_shown") is not False
            )
            self.statusChanged.emit()

        def failed(message: str) -> None:
            # The checkbox's enabled state follows its own tick, so put the
            # saved value back on screen instead of leaving it greyed out.
            self._operation_failed(message)
            self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_ancs_notification_actions(enabled),
            completed,
            failed,
        )

    @Slot(bool)
    def setMirrorNotificationRemovals(self, enabled: bool) -> None:
        """Whether a removal on the iPhone also closes the desktop popup."""
        def completed(value: object) -> None:
            self._status["mirror_iphone_removals"] = bool(value)
            self.statusChanged.emit()

        def failed(message: str) -> None:
            # Put the saved value back on screen after a failed save.
            self._operation_failed(message)
            self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_mirror_notification_removals(bool(enabled)),
            completed,
            failed,
        )

    @Slot(bool, int)
    def setProximityLock(self, enabled: bool, grace_seconds: int) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_proximity_lock(
                bool(enabled), int(grace_seconds)
            ),
            completed,
        )

    @Slot(bool, bool)
    def setCallHistory(self, enabled: bool, missed_call_notifications: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_call_history(
                bool(enabled), bool(missed_call_notifications)
            ),
            completed,
        )

    def _open_map_updated(self, value: object) -> None:
        if isinstance(value, list):
            self._notification_open_map = [dict(rule) for rule in value if isinstance(rule, dict)]
            self.notificationOpenMapChanged.emit()

    @Slot()
    def loadNotificationOpenMap(self) -> None:
        self._run(
            self._backend.notification_open_map, self._open_map_updated, busy=False,
        )

    @Slot(str, str)
    def setNotificationOpenTarget(self, bundle_id: str, target: str) -> None:
        bundle = str(bundle_id or "").strip()
        selected = str(target or "").strip()
        if not bundle or not selected:
            return
        self._run(
            lambda: self._backend.set_notification_open_target(bundle, selected),
            self._open_map_updated,
        )

    @Slot(str)
    def removeNotificationOpenTarget(self, bundle_id: str) -> None:
        bundle = str(bundle_id or "").strip()
        if not bundle:
            return
        self._run(
            lambda: self._backend.remove_notification_open_target(bundle),
            lambda _removed: self.loadNotificationOpenMap(),
        )

    @Slot(bool)
    def setMediaControl(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
            self.refreshNowPlaying()

        self._run(lambda: self._backend.set_media_control(bool(enabled)), completed)

    @Slot(bool)
    def setPhoneBatteryWarning(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            self._status["phone_battery_warning"] = bool(value)
            self.statusChanged.emit()

        self._run(lambda: self._backend.set_phone_battery_warning(bool(enabled)), completed)

    @Slot(bool)
    def setAutoProfileReset(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            self._status["profile_reset_auto"] = bool(value)
            self.statusChanged.emit()

        self._run(lambda: self._backend.set_auto_profile_reset(bool(enabled)), completed)

    @Slot(bool)
    def setContactPhotos(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                if value.get("contact_photos") is not True:
                    for key in ("contact_photo_revision", "contact_photo_count"):
                        self._status.pop(key, None)
                self._status.update(value)
                self.statusChanged.emit()
                self._sync_avatars()

        self._run(lambda: self._backend.set_contact_photos(bool(enabled)), completed)

    @Slot(bool)
    def setOtpAutocopy(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()

        self._run(lambda: self._backend.set_otp_autocopy(bool(enabled)), completed)

    @Slot(bool)
    def setCallsEnabled(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
                self.refreshCalls()

        self._run(lambda: self._backend.set_calls_enabled(bool(enabled)), completed)

    @Slot(bool)
    def setMprisPlayer(self, enabled: bool) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()

        self._run(lambda: self._backend.set_mpris_player(bool(enabled)), completed)

    @Slot(str)
    def setStoragePolicy(self, policy: str) -> None:
        if policy == "encrypted":
            # SetStoragePolicy already opens the wallet when encryption is
            # selected, so do not immediately issue a duplicate request.
            self._storage_unlock_attempted = True

        self._run(lambda: self._backend.set_storage_policy(policy), self._storage_updated)

    @Slot()
    def unlockStorage(self) -> None:
        self._storage_unlock_attempted = True

        self._run(self._backend.unlock_storage, self._storage_updated)

    def _storage_updated(self, value: object) -> None:
        if isinstance(value, dict):
            self._status.update(value)
            self._state.status = BackendStatus.from_dict(self._status)
            self.statusChanged.emit()
        self.refresh()

    def _maybe_unlock_storage(self) -> None:
        if self._storage_unlock_attempted:
            return
        if (
            self._status.get("daemon")
            and self._status.get("storage_policy") == "encrypted"
            and self._status.get("storage_state") != "ready"
        ):
            self._storage_unlock_attempted = True
            self.unlockStorage()

    @Slot()
    def loadBluetoothStatus(self) -> None:
        def completed(value: object) -> None:
            self._bluetooth_active = bool(getattr(value, "active", False))
            self.bluetoothChanged.emit()
            self.loadSetupState()

        self._run(self._setup.bluez_status, completed, busy=False)

    @Slot(bool)
    def loadDevices(self, scan: bool) -> None:
        def completed(value: object) -> None:
            devices = list(value) if isinstance(value, list) else []
            adapter = str(self._compatibility.get("adapter", ""))
            candidates = iphone_candidates(
                devices,
                adapter=adapter,
                configured_mac=self._configuration.mac,
                include_unpaired=scan,
            )
            self._devices = []
            for item in candidates:
                value = item.to_dict()
                if item.paired:
                    value["display_name"] = _("{name} — paired").format(name=item.name)
                else:
                    value["display_name"] = item.name
                self._devices.append(value)
            self.devicesChanged.emit()
            if scan and not self._devices:
                self._set_error(
                    _(
                        "No Bluetooth devices found; unlock the iPhone and keep "
                        "Bluetooth settings open"
                    )
                )

        adapter = str(self._compatibility.get("adapter", "")).strip() or None
        self._run(
            lambda: self._setup.devices(
                scan_seconds=DISCOVERY_SECONDS if scan else 0, adapter=adapter,
            ),
            completed,
        )

    @Slot()
    def activateBluetooth(self) -> None:
        def completed(_value: object) -> None:
            self._reload_setup_state(scan_after=True)

        self._run(self._setup.activate_bluez, completed)

    @Slot(str, bool, bool)
    def completePairing(
        self,
        mac: str,
        compatibility_mode: bool = False,
        explicit_pairing: bool = False,
    ) -> None:
        self._start_pairing(
            mac,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
        )

    @Slot(str, str, bool, bool)
    def replaceAndPair(
        self,
        previous_mac: str,
        mac: str,
        compatibility_mode: bool = False,
        explicit_pairing: bool = False,
    ) -> None:
        self._start_pairing(
            mac,
            replace_saved_mac=previous_mac,
            compatibility_mode=compatibility_mode,
            explicit_pairing=explicit_pairing,
        )

    def _start_pairing(
        self,
        mac: str,
        *,
        replace_saved_mac: str = "",
        compatibility_mode: bool = False,
        explicit_pairing: bool = False,
    ) -> None:
        def completed(value: object) -> None:
            self._configuration = ConfigurationState(
                configured=True,
                mac=mac,
                adapter=str(self._compatibility.get("adapter", "")),
                path="",
                saved=True,
                bonded=True,
                ancs_enabled=bool(getattr(value, "ancs_enabled", True)),
            )
            self.configuredChanged.emit()
            self.compatibilityChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)
            self.loadSetupState()
            self.refresh()
            self._refresh_pairing_issue_report()

        def confirm(passkey: int | None) -> bool:
            event = threading.Event()
            decision = [False]
            with self._pairing_confirmation_lock:
                self._pairing_confirmation = (event, decision)
            self.pairingConfirmationRequested.emit(
                f"{passkey:06d}" if passkey is not None else ""
            )
            answered = event.wait(60.0)
            with self._pairing_confirmation_lock:
                if self._pairing_confirmation is not None:
                    pending_event, _pending_decision = self._pairing_confirmation
                    if pending_event is event:
                        self._pairing_confirmation = None
            return answered and decision[0]

        def display(passkey: int) -> None:
            # RequestConfirmation normally follows DisplayPasskey. The former
            # opens the actionable dialog and carries the same numeric code.
            # Keeping this callback present gives BlueZ DisplayYesNo capability
            # without prompting the user twice.
            _ = passkey

        def operation():
            adapter = str(self._compatibility.get("adapter", "")).strip() or None
            wanted = mac.strip().upper()
            for item in self._devices:
                if str(item.get("mac", "")).upper() != wanted:
                    continue
                path = str(item.get("adapter_path", ""))
                if path:
                    adapter = path.rsplit("/", 1)[-1]
                break
            options = {
                "confirmation": confirm,
                "display": display,
                "adapter": adapter,
                "replace_saved_mac": replace_saved_mac,
            }
            if compatibility_mode:
                options["compatibility_mode"] = True
            if explicit_pairing:
                options["explicit_pairing"] = True
            return self._setup.complete_isolated(mac, **options)

        def failed(message: str) -> None:
            self._operation_failed(message)
            self._refresh_pairing_issue_report()
            if replace_saved_mac:
                self.loadSetupState()
                self.loadDevices(False)

        self._run(
            # PairingAgent callbacks require a dispatching GLib D-Bus loop.
            # Qt setup work runs on a worker whose private dbus-python
            # connection deliberately uses NULL_MAIN_LOOP, so host the agent
            # in the same isolated helper used by the GTK client.
            operation,
            completed,
            failed,
        )

    @Slot(bool)
    def answerPairingConfirmation(self, accepted: bool) -> None:
        with self._pairing_confirmation_lock:
            pending = self._pairing_confirmation
            self._pairing_confirmation = None
        if pending is None:
            return
        event, decision = pending
        decision[0] = accepted
        event.set()

    @Slot(str)
    def forgetDevice(self, mac: str) -> None:
        def completed(_value: object) -> None:
            self._configuration = ConfigurationState(False, "", "", "")
            self._status = {}
            self._threads = []
            self._state = ConversationState(select_first=False)
            self._pending_group = None
            self._contact_results = []
            self.contactResultsChanged.emit()
            self.configuredChanged.emit()
            self.compatibilityChanged.emit()
            self.statusChanged.emit()
            self._sync_avatars()
            self.threadsChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)
            self.loadSetupState()

        adapter = self._configuration.adapter.strip() or None
        self._run(lambda: self._setup.forget(mac, adapter=adapter), completed)


def _photo_item(photo: Photo) -> dict[str, object]:
    """One grid cell; paths become file URLs, text stays plain."""
    from PySide6.QtCore import QUrl

    def url(path: object) -> str:
        return QUrl.fromLocalFile(str(path)).toString() if path else ""

    return {
        "id": photo.id,
        "label": photos_view.label(photo),
        "video": photo.type == "video",
        "thumbnail": url(photo.thumbnail),
        "original": url(photo.original),
    }
