"""Asynchronous presentation controller for the Kirigami client."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

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

from blueferry import (
    __version__,
    call_audio_view,
    contacts_view,
    doctor_report,
    features_view,
    file_drop_view,
    media_volume_view,
    notification_apps_view,
    notification_pin_view,
    phone_audio_view,
    phone_link_view,
    phone_overview,
    search_view,
)
from blueferry.backend_lifecycle import ensure_backend_current, restart_backend
from blueferry.bluetooth_devices import iphone_candidates
from blueferry.client import BackendClient, BackendError, TetherUnsupportedError
from blueferry.contacts_model import ContactBook
from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
    fetch_conversation_snapshot,
)
from blueferry.doctor_report import DoctorReport, DoctorRunner
from blueferry.i18n import _
from blueferry.limits import MAX_CALL_HISTORY_QUERY_LIMIT
from blueferry.models import BackendStatus, CallsSnapshot, Thread
from blueferry.notification_history import NotificationHistory
from blueferry.onboarding import OnboardingState, effective_compatibility
from blueferry.photos_model import PhotosTabModel
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH, TETHER_IFACE
from blueferry.qt.avatars import avatar_url
from blueferry.qt.companion import CompanionTools
from blueferry.qt.plugin_settings import PluginSettings
from blueferry.qt.plugin_surfaces import PluginSurfaces
from blueferry.qt.tasks import QtTaskRunner, Task, qt_timers
from blueferry.qt.thumbnails import thumbnail_url
from blueferry.quirks_report import issue_report, issue_url
from blueferry.reconnect_view import ManualReconnect, reconnect_view
from blueferry.search_model import GlobalSearch
from blueferry.service_manager import bluetooth_restart_command
from blueferry.setup_client import (
    DISCOVERY_SECONDS,
    ConfigurationState,
    SetupClient,
)
from blueferry.tel_links import dial_request, known_contact_name
from blueferry.tether_status import TetherStatus


def _pin_failure_text(error: BackendError) -> str:
    """The shared pin error text, from the D-Bus error behind ``error``."""
    cause = error.__cause__
    get_name = getattr(cause, "get_dbus_name", None)
    get_message = getattr(cause, "get_dbus_message", None)
    if callable(get_name):
        message = get_message() if callable(get_message) else ""
        return notification_pin_view.pin_error_text(get_name(), message)
    return notification_pin_view.pin_error_text("", str(error))


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
# Avatar-provider key of the open contact card's photo; never a phone number
# or e-mail address, so it cannot collide with a conversation avatar.
CARD_PHOTO_PREFIX = "contact-card:"


class _WorkerRunner:
    """:class:`blueferry.client_tasks.TaskRunner` on the controller's
    serialized D-Bus worker (``BridgeController._run``), with or without the
    busy indicator. ``_run`` is looked up per call, so tests that replace it
    run these tasks inline too."""

    def __init__(self, controller: BridgeController, *, busy: bool = False) -> None:
        self._controller = controller
        self._busy = busy

    def submit(
        self,
        operation: Callable[[], object],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        controller = self._controller
        if on_finished is None:
            controller._run(operation, on_done, on_error, busy=self._busy)
            return

        def done(value: object) -> None:
            try:
                if on_done is not None:
                    on_done(value)
            finally:
                on_finished()

        def failed(message: str) -> None:
            try:
                (on_error or controller._operation_failed)(message)
            finally:
                on_finished()

        controller._run(operation, done, failed, busy=self._busy)


# Private key of the muted app IDs added to a ListNotifications answer.
# Calls the Recent Calls page reads at first.
CALL_HISTORY_PAGE = 200
_MUTED_APPS_KEY = "_blueferry_muted_apps"


# What the rows need from the raw ListNotifications answer: never the titles
# or bodies, which go with the shared model's rows.
_RECORD_PIN_FIELDS = ("key", "pinned", "removed", "actionable", "app_id")


def _pin_fields(value: object) -> dict:
    """The pin keys, flags and app IDs of one ListNotifications answer."""
    if not isinstance(value, dict):
        return {}
    records = value.get("notifications")
    return {
        "pins_max": value.get("pins_max"),
        _MUTED_APPS_KEY: value.get(_MUTED_APPS_KEY) or frozenset(),
        "notifications": [
            {name: record[name] for name in _RECORD_PIN_FIELDS if name in record}
            if isinstance(record, Mapping) else None
            for record in records
        ] if isinstance(records, list) else [],
    }


class _NotificationRunner:
    """The TaskRunner of the Notifications tab's shared model.

    On the GUI thread it decides, before the work is queued, whether the
    muted apps are read too, and keeps each raw ListNotifications answer
    beside the model's display rows: the answer is taken just before the
    model sees it (the model re-renders from inside ``on_done``) and put
    back to the previous one when the model drops the reply (tab hidden
    meanwhile, or a newer read superseded it), so rows and pin keys always
    come from the same answer.
    """

    def __init__(self, controller: BridgeController) -> None:
        self._controller = controller
        self._runner = _WorkerRunner(controller)

    def submit(
        self,
        operation: Callable[[], object],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        controller = self._controller
        # Read GetStatus here, never on the D-Bus worker.
        controller._notifications_with_muted_apps = notification_apps_view.supported(
            controller._status
        )

        def deliver(value: object, callback: Callable[[], None]) -> None:
            history = controller._notification_history
            rows = history.rows
            previous = controller._notification_reply
            controller._notification_reply = _pin_fields(value)
            callback()
            if history.rows is rows:
                controller._notification_reply = previous

        def done(value: object) -> None:
            deliver(value, lambda: on_done(value) if on_done is not None else None)

        def failed(message: str) -> None:
            deliver(None, lambda: on_error(message) if on_error is not None else None)

        self._runner.submit(operation, done, failed, on_finished)


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
    # A reply to a message that may be from a group: key, draft, name, address.
    senderConfirmationRequested = Signal(str, str, str, str)
    # A reply to a group whose members are unconfirmed: key and draft.
    groupParticipantsRequested = Signal(str, str)
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
    fileDropChanged = Signal()
    # A short notice about files dropped on the window: text, ok.
    fileDropNotice = Signal(str, bool)
    notificationAppsChanged = Signal()
    contactsChanged = Signal()
    searchChanged = Signal()
    phoneIdentityChanged = Signal()
    featuresChanged = Signal()
    doctorChanged = Signal()
    phoneCallsChanged = Signal()
    callAudioDevicesChanged = Signal()
    callTonesDraftChanged = Signal()
    callTonesSentChanged = Signal()
    tetherChanged = Signal()
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
        doctor: Callable[[], DoctorReport] | None = None,
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
        # Whether the last prompt asked to reply to the sender alone, so an
        # answer that no longer matches it gets the sender's wording.
        self._confirming_sender = False
        self._contact_results: list[dict] = []
        self._state = ConversationState(select_first=False)
        self._status: dict = {}
        self._notification_open_map: list[dict] = []
        self._devices: list[dict] = []
        # Settings: local.env switches (Messages1.GetFeatures) and the doctor
        # report; the doctor runs a local program on its own pool so the
        # D-Bus worker stays free.
        self._features: dict = features_view.initial()
        self._features_revision = 0
        self._feature_saving: set[str] = set()
        self._doctor_process = DoctorRunner()
        self._doctor_runner = doctor or self._doctor_process.run
        self._doctor: dict = doctor_report.idle_state()
        self._local_pool = QThreadPool(self)
        self._local_pool.setMaxThreadCount(1)
        self._bluetooth_active = False
        self._busy_count = 0
        self._error_text = ""
        self._manual_reconnect = ManualReconnect()
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
        # The keypad-tone field's text: the call cards are rebuilt whenever
        # phoneCalls changes, so the draft lives here, not in the delegate.
        self._call_tones_draft = ""
        # Keypad clicks, keyboard keys and the tone field share one ordered
        # queue (phone_link_view.KeypadTones, as in GTK and the terminal).
        self._keypad = phone_link_view.KeypadTones(
            self._send_tones, on_changed=self.callTonesSentChanged.emit,
            on_error=self._operation_failed,
        )
        self._calls_state = "disabled"
        # ListCalls "audio" (the call's speaker and microphone) and the
        # device lists from ListCallAudioDevices, read on demand.
        self._phone_call_audio: dict = {}
        self._call_audio_devices: dict = {}
        self._call_audio_loading = False
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
        # Recent calls read per load; a search hit can reach further back.
        self._call_history_limit = CALL_HISTORY_PAGE
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
        # The shared model keeps display rows; the raw answer (pin keys and
        # flags, in row order) and the muted apps are kept beside it.
        self._notification_reply: dict = {}
        self._notification_snapshot: dict = {}
        self._notification_rows: list[dict] = []
        self._pin_error = ""
        self._notifications_with_muted_apps = False
        self._notification_history = NotificationHistory(
            runner=_NotificationRunner(self),
            timer=qt_timers(self),
            fetch=self._fetch_notifications,
            on_change=self._notifications_updated,
        )
        self._notifications_info: dict = self._compose_notifications_info()
        self.statusChanged.connect(
            lambda: self._notification_history.sync_status(self._status))
        self.devicesChanged.connect(self.phoneIdentityChanged)
        self.configuredChanged.connect(self.phoneIdentityChanged)
        # The Photos tab (a plugin with the photos capability).
        self._photos_pool = QThreadPool(self)
        self._photos_pool.setMaxThreadCount(1)
        self._photos = PhotosTabModel(
            runner=QtTaskRunner(self._photos_pool),
            url=_file_url, thumbnail_url=thumbnail_url, open_file=_open_file,
            on_change=self.photosChanged.emit, on_error=self._operation_failed,
        )
        # UxPlay, LocalSend and iPhone photos: client-side, no daemon involved.
        self._companion = companion or CompanionTools(parent=self)
        self._companion.changed.connect(self.companionToolsChanged)
        # Plugin management runs git, pip and plugin calls on its own queue.
        self._plugin_settings = plugin_settings or PluginSettings(parent=self)
        self._plugin_settings.changed.connect(self.pluginSettingsChanged)
        # Settings > Notifications > Muted apps: fetched only while shown.
        self._notification_apps: dict = self._empty_notification_apps()
        self._notification_apps_watched = False
        self._notification_apps_timer = QTimer(self)
        self._notification_apps_timer.setSingleShot(True)
        self._notification_apps_timer.setInterval(150)
        self._notification_apps_timer.timeout.connect(self.refreshNotificationApps)
        # Contacts tab (read only while shown) and the global search; both
        # use the shared models, the D-Bus calls run on the worker.
        self._card_photo: tuple[str, bytes] = ("", b"")
        self._card_photo_serial = 0
        self._contact_book = ContactBook(
            runner=_WorkerRunner(self), timer=qt_timers(self),
            fetch_page=lambda offset, limit, query: self._backend.contact_cards(
                offset, limit, query),
            fetch_card=lambda contact_id: self._backend.contact(contact_id),
            on_change=self._contacts_updated,
        )
        self._search = GlobalSearch(
            runner=_WorkerRunner(self), timer=qt_timers(self),
            fetch=lambda query, limit: self._backend.search(query, limit),
            on_change=self.searchChanged.emit,
        )
        self.statusChanged.connect(self._status_for_contacts)
        self._tether: dict = {"available": False}
        self._tether_pending = False
        self._tether_timer = QTimer(self)
        self._tether_timer.setSingleShot(True)
        self._tether_timer.setInterval(100)
        self._tether_timer.timeout.connect(self.refreshTether)
        self._bus = QDBusConnection.sessionBus() if subscribe else None
        # "From Plugins" and "Send to…", on their own pool.
        self._plugin_surfaces = plugin_surfaces or PluginSurfaces(
            bus=self._bus, parent=self,
        )
        self._plugin_surfaces.changed.connect(self.pluginSurfacesChanged)
        self._plugin_surfaces.dropChanged.connect(self.fileDropChanged)
        self._plugin_surfaces.dropNotice.connect(self.fileDropNotice)
        # Enabling, installing or removing a plugin changes the card. None
        # until the first plugin list has loaded; an empty tuple means "no
        # plugin installed", so installing the very first one still reloads.
        self._plugin_set: tuple | None = None
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

    @Property("QVariantList", notify=contactResultsChanged)
    def dialSuggestions(self):
        """The contact matches the Calls tab offers (phone numbers only)."""
        return phone_link_view.dial_suggestions(self._contact_results)

    @Property("QVariantList", notify=phoneCallsChanged)
    def phoneCalls(self):
        return self._phone_calls

    @Property(str, notify=phoneCallsChanged)
    def callsState(self) -> str:
        return self._calls_state

    @Property("QVariantMap", notify=phoneCallsChanged)
    def phoneCallAudio(self):
        """ListCalls ``audio``: the running call's speaker and microphone."""
        return self._phone_call_audio

    @Property("QVariantMap", notify=callAudioDevicesChanged)
    def callAudioDevices(self):
        return self._call_audio_devices

    @Property(str, notify=phoneCallsChanged)
    def activeCallId(self) -> str:
        """The call keypad tones go to; "" while no call is connected."""
        return phone_link_view.active_call_id(self._phone_calls)

    @Property(str, notify=callTonesSentChanged)
    def callTonesSent(self) -> str:
        """"Sent: …" under the active call, or ""."""
        return phone_link_view.tones_feedback(self._keypad.sent)

    @Property(str, notify=callTonesDraftChanged)
    def callTonesDraft(self) -> str:
        return self._call_tones_draft

    @Slot(str)
    def setCallTonesDraft(self, text: str) -> None:
        draft = phone_link_view.dial_tones(text)
        if draft != self._call_tones_draft:
            self._call_tones_draft = draft
            self.callTonesDraftChanged.emit()

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
            "sync": audio["sync"],
        }

    @Property("QVariantMap", notify=statusChanged)
    def reconnect(self):
        """Manual reconnect: available, offered (button), attention, hint."""
        view = reconnect_view(self._status)
        return {
            "available": view.available,
            "offered": view.offered,
            "attention": view.attention,
            "hint": self._manual_reconnect.hint(view),
        }

    @Slot()
    def reconnectPhone(self) -> None:
        """Skip the automatic backoff and page the iPhone once now."""
        def completed(value: object) -> None:
            error = self._manual_reconnect.answered(value)
            if error:
                self._set_error(error)
            self.statusChanged.emit()
            self.refresh()

        self._run(self._backend.reconnect_phone, completed, busy=False)

    def _follow_manual_reconnect(self) -> None:
        """Turn the status outcome of a manual attempt into a clear message."""
        error = self._manual_reconnect.follow(self._status)
        if error:
            self._set_error(error)

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
            if address.startswith(CARD_PHOTO_PREFIX):
                key, data = self._card_photo
                return data if data and address == key else None
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
        return phone_link_view.feature_hints(self._status)

    @Property(str, notify=phoneIdentityChanged)
    def phoneName(self) -> str:
        return phone_link_view.phone_name(self._devices, self._configuration.mac)

    @Property(str, notify=statusChanged)
    def phoneConnection(self) -> str:
        return phone_link_view.connection_text(self._status)

    @Property("QVariantList", constant=True)
    def dialKeys(self):
        """The dial pad's keys as ``[key, letters]`` pairs."""
        return [list(key) for key in phone_link_view.DIAL_KEYS]

    @Slot(str, result=str)
    def dialable(self, value: str) -> str:
        return phone_link_view.dialable(value)

    @Slot("QVariantMap", str, result=str)
    def callsHint(self, status: dict, calls_state: str) -> str:
        """Why the dial pad cannot call; QML passes the properties it binds."""
        return phone_link_view.calls_hint(dict(status or {}), str(calls_state or ""))

    @Slot("QVariantMap", float, result="QVariantMap")
    def callCard(self, call: dict, now_ms: float) -> dict:
        """A running call's card; QML passes its 1 s ticker in milliseconds."""
        return phone_link_view.call_card(dict(call or {}), float(now_ms) / 1000)

    @Slot("QVariantMap", "QVariantMap", result="QVariantMap")
    def callAudioSettingsView(self, status: dict, devices: dict) -> dict:
        """Settings rows for the call speaker/microphone (call_audio_view)."""
        return call_audio_view.settings_view(dict(status or {}), dict(devices or {}))

    @Slot("QVariantMap", "QVariantMap", result="QVariantMap")
    def callAudioView(self, audio: dict, devices: dict) -> dict:
        """The running call's devices and the per-call switch (call_audio_view)."""
        return call_audio_view.call_view(dict(audio) if audio else None, dict(devices or {}))

    @Slot("QVariantMap", str, result="QVariantMap")
    def notificationsView(self, info: dict, opt_in_hint: str) -> dict:
        return phone_link_view.notifications_view(dict(info or {}), str(opt_in_hint or ""))

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
        state = self._plugin_settings.state()
        if not state.get("loaded"):
            return
        current = tuple(
            (row.get("id"), row.get("enabled")) for row in state.get("plugins", [])
        )
        if current != self._plugin_set:
            known = self._plugin_set is not None
            self._plugin_set = current
            if known:
                self._plugin_surfaces.schedule_reload()
                # The photo plugin may be gone or disabled now: forget it
                # until the next load finds it again.
                self._photos.plugins_changed()

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

    @Property("QVariantMap", notify=fileDropChanged)
    def fileDrop(self):
        """Files dropped on the window (file_drop_view.py): phase (idle,
        checking, picking, none, sending), choices with key/label/icon,
        selected, title, body and the fixed texts; all plain text."""
        return {
            **self._plugin_surfaces.file_drop.state(),
            "overlayTitle": file_drop_view.overlay_title(),
            "overlaySubtitle": file_drop_view.overlay_subtitle(),
            "settingsLabel": file_drop_view.settings_label(),
            "sendLabel": file_drop_view.send_label(),
        }

    @Slot("QVariantList")
    def dropFiles(self, urls: list) -> None:
        """Files dropped anywhere on the main window."""
        self._plugin_surfaces.drop(list(urls or []))

    @Slot(str)
    def pickDropTarget(self, key: str) -> None:
        self._plugin_surfaces.file_drop.pick(str(key))

    @Slot()
    def cancelDrop(self) -> None:
        self._plugin_surfaces.file_drop.cancel()

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
        return self._photos.state

    @Slot(bool)
    def watchPhotos(self, watched: bool) -> None:
        """The Photos tab became visible (load) or hidden (forget the list)."""
        self._photos.watch(bool(watched))

    @Slot()
    def refreshPhotos(self) -> None:
        self._photos.refresh()

    @Slot(str)
    def openPhoto(self, photo_id: str) -> None:
        """Download the original (plugin) and open it in the default viewer."""
        self._photos.open(photo_id)

    def _fetch_notifications(self, limit: int) -> object:
        """ListNotifications plus, on daemons that can mute, the muted apps
        (to dim their rows); runs on the D-Bus worker."""
        snapshot = self._backend.notifications(limit)
        if not isinstance(snapshot, dict) or not self._notifications_with_muted_apps:
            return snapshot
        try:
            muted = notification_apps_view.muted_ids(self._backend.notification_apps())
        except BackendError:
            muted = frozenset()
        return {**snapshot, _MUTED_APPS_KEY: muted}

    def _notifications_updated(self) -> None:
        """The shared model changed: add the pin fields and the muted flag
        to its display rows (same order as the raw records)."""
        history = self._notification_history
        if not history.rows:
            # Forgotten rows (tab hidden, storage locked, list off) take
            # their keys along.
            self._notification_reply = {}
        snapshot = self._notification_reply
        records = snapshot.get("notifications")
        records = [r for r in records if isinstance(r, Mapping)] if isinstance(
            records, list
        ) else []
        muted = snapshot.get(_MUTED_APPS_KEY) or frozenset()
        rows: list[dict] = []
        for index, row in enumerate(history.rows):
            record = records[index] if index < len(records) else {}
            app_id = record.get("app_id")
            app_id = app_id if isinstance(app_id, str) else ""
            rows.append({
                **notification_pin_view.pin_row_extras(record),
                **row,
                "app_id": app_id,
                "muted": bool(app_id) and app_id in muted,
            })
        self._notification_rows = rows
        self._notification_snapshot = snapshot
        self._update_notifications_info()

    def _compose_notifications_info(self) -> dict:
        snapshot = self._notification_snapshot
        return {
            **self._notification_history.info,
            "pinLimitReached": notification_pin_view.pin_limit_reached(snapshot),
            "pinLimitText": notification_pin_view.pin_limit_text(snapshot.get("pins_max")),
            "pinError": self._pin_error,
        }

    def _update_notifications_info(self) -> None:
        self._notifications_info = self._compose_notifications_info()
        self.notificationsChanged.emit()

    @Property("QVariantList", notify=notificationsChanged)
    def notifications(self):
        return self._notification_rows

    @Property("QVariantMap", notify=notificationsChanged)
    def notificationsInfo(self):
        return self._notifications_info

    @Slot(bool)
    def watchNotifications(self, watched: bool) -> None:
        """The Notifications tab became visible (load) or hidden (forget)."""
        if watched:
            self._notification_history.watch(True)
            return
        # Titles and bodies of the raw answer go with the rows.
        self._notification_reply = {}
        self._pin_error = ""
        rows = self._notification_history.rows
        self._notification_history.watch(False)
        if self._notification_history.rows is rows:
            # The model had no rows to forget and so reported no change.
            self._notifications_updated()

    @Slot()
    def refreshNotifications(self) -> None:
        self._notification_history.refresh()

    @Slot()
    def _notificationsInvalidated(self) -> None:
        self._notification_history.invalidated()

    def _set_pin_error(self, text: str) -> None:
        if text != self._pin_error:
            self._pin_error = text
            self._update_notifications_info()

    @Slot(str, bool)
    def setNotificationPinned(self, key: str, pinned: bool) -> None:
        """Pin or unpin one listed notification (Messages1.SetNotificationPinned)."""
        selected = str(key or "")
        if not selected:
            return
        wanted = bool(pinned)

        def operation() -> object:
            try:
                return self._backend.set_notification_pinned(selected, wanted)
            except BackendError as error:
                raise BackendError(_pin_failure_text(error)) from error

        def completed(_value: object) -> None:
            self._set_pin_error("")
            self.refreshNotifications()

        def failed(message: str) -> None:
            self._set_pin_error(message or notification_pin_view.GENERIC_ERROR_TEXT)
            # An unknown key means the list moved on; show the current one.
            self.refreshNotifications()

        self._run(operation, completed, failed, busy=False)

    @staticmethod
    def _empty_notification_apps() -> dict:
        return {
            "loaded": False,
            "available": False,
            "label": notification_apps_view.section_label(),
            "description": notification_apps_view.section_description(),
            "hint": "",
            "rows": [],
            "count": 0,
            "error": "",
        }

    @Property(bool, notify=statusChanged)
    def notificationAppsSupported(self) -> bool:
        """Whether the running daemon can mute single iPhone apps."""
        return notification_apps_view.supported(self._status)

    @Property("QVariantMap", notify=notificationAppsChanged)
    def notificationApps(self):
        return self._notification_apps

    @Slot(bool)
    def watchNotificationApps(self, watched: bool) -> None:
        """Settings > Notifications shows (load) or hides (forget) the muted apps."""
        self._notification_apps_watched = bool(watched)
        if self._notification_apps_watched:
            self.refreshNotificationApps()
            return
        self._notification_apps_timer.stop()
        if self._notification_apps != self._empty_notification_apps():
            self._notification_apps = self._empty_notification_apps()
            self.notificationAppsChanged.emit()

    @Slot()
    def refreshNotificationApps(self) -> None:
        self._read_notification_apps(keep_error=False)

    def _read_notification_apps(self, *, keep_error: bool) -> None:
        if not self._notification_apps_watched or not self.notificationAppsSupported:
            return

        def completed(value: object) -> None:
            if not self._notification_apps_watched:
                return
            view = notification_apps_view.list_view(value)
            error = self._notification_apps.get("error", "") if keep_error else ""
            self._notification_apps = {**view, "loaded": True, "error": error}
            self.notificationAppsChanged.emit()

        def failed(message: str) -> None:
            if not self._notification_apps_watched:
                return
            self._notification_apps = {
                **self._empty_notification_apps(),
                "error": message or _("Could not read the muted apps."),
            }
            self.notificationAppsChanged.emit()

        self._run(self._backend.notification_apps, completed, failed, busy=False)

    @Slot()
    def _notificationAppsInvalidated(self) -> None:
        # Content-free signal: refetch only what is on screen.
        if self._notification_apps_watched:
            self._notification_apps_timer.start()
        self._notification_history.invalidated()

    @Slot(str, bool)
    def setNotificationAppMuted(self, app_id: str, muted: bool) -> None:
        """Mute or unmute the desktop popups of one iPhone app."""
        selected = str(app_id or "")
        if not selected:
            return
        wanted = bool(muted)

        def completed(_value: object) -> None:
            self._notification_apps = {**self._notification_apps, "error": ""}
            self.refreshNotificationApps()
            self._notificationsInvalidated()

        def failed(message: str) -> None:
            # Re-emit so the switch shows the saved state again.
            self._notification_apps = {
                **self._notification_apps,
                "error": notification_apps_view.change_failed_text(message),
            }
            self.notificationAppsChanged.emit()
            # Muting may have become unavailable (settings.json unreadable,
            # storage locked): read the list again for its switches and hint.
            self._read_notification_apps(keep_error=True)

        self._run(
            lambda: self._backend.set_notification_app_muted(selected, wanted),
            completed,
            failed,
        )

    # ---- Contacts tab and global search ---------------------------------

    @Slot()
    def _status_for_contacts(self) -> None:
        self._contact_book.apply_status(self._status)
        self._search.apply_status(self._status)

    def _contacts_updated(self) -> None:
        book = self._contact_book
        photo = book.photo if book.card["open"] else b""
        with self._avatar_lock:
            if not photo:
                self._card_photo = ("", b"")
            elif photo is not self._card_photo[1]:
                # A new key per photo, so QML never shows a cached older one.
                self._card_photo_serial += 1
                self._card_photo = (f"{CARD_PHOTO_PREFIX}{self._card_photo_serial}", photo)
        self.contactsChanged.emit()

    @Property(bool, notify=statusChanged)
    def contactsSupported(self) -> bool:
        """Whether the running daemon offers contact cards."""
        return contacts_view.supported(self._status)

    @Property("QVariantMap", notify=contactsChanged)
    def contacts(self):
        """The Contacts list: rows, count and empty texts, paging state."""
        return self._contact_book.view()

    @Property("QVariantMap", notify=contactsChanged)
    def contactCard(self):
        """The open card (``contacts_view.card_view``) and its photo URL."""
        key = self._card_photo[0]
        return {
            **self._contact_book.card,
            "photo": avatar_url(key, 0) if key else "",
        }

    @Slot(bool)
    def watchContacts(self, watched: bool) -> None:
        """The Contacts tab is shown (load) or hidden (forget the cards)."""
        self._contact_book.watch(watched)

    @Slot(str)
    def setContactQuery(self, query: str) -> None:
        self._contact_book.set_query(query)

    @Slot()
    def reloadContacts(self) -> None:
        self._contact_book.reload()

    @Slot()
    def loadMoreContacts(self) -> None:
        self._contact_book.load_more()

    @Slot(str)
    def openContact(self, contact_id: str) -> None:
        self._contact_book.open(contact_id)

    @Slot()
    def closeContact(self) -> None:
        self._contact_book.close()

    @Property(bool, notify=statusChanged)
    def searchSupported(self) -> bool:
        """Whether the running daemon offers the global search."""
        return search_view.supported(self._status)

    @Property("QVariantMap", notify=searchChanged)
    def searchResults(self):
        """``search_view.search_view`` plus state; highlights as [start, length]."""
        view = self._search.view()
        sections = [
            {**section, "rows": [
                {**row, "highlight": list(row["highlight"] or [])}
                for row in section["rows"]
            ]}
            for section in view["sections"]
        ]
        return {**view, "sections": sections}

    @Slot(str)
    def setSearchQuery(self, query: str) -> None:
        self._search.set_query(query)

    @Slot()
    def clearSearch(self) -> None:
        self._search.clear()

    @Property(str, notify=callHistoryChanged)
    def callHistoryError(self) -> str:
        return self._call_history_error
    @Property("QVariantList", notify=notificationOpenMapChanged)
    def notificationOpenMap(self):
        return self._notification_open_map

    @Property("QVariantMap", notify=nowPlayingChanged)
    def nowPlaying(self):
        return self._now_playing

    @Property("QVariantMap", notify=nowPlayingChanged)
    def mediaVolume(self):
        """iPhone volume buttons and level (blueferry.media_volume_view)."""
        return media_volume_view.volume_controls(self._now_playing)

    @Property("QVariantMap", notify=tetherChanged)
    def tether(self):
        return self._tether

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
            "NotificationAppsChanged",
            self,
            SLOT("_notificationAppsInvalidated()"),
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
        self._bus.connect(
            BUS_NAME,
            OBJECT_PATH,
            TETHER_IFACE,
            "TetherChanged",
            self,
            SLOT("_tetherInvalidated()"),
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
        # Shown search hits may name messages that changed or are gone.
        self._search.invalidate()

    @Slot()
    def _statusInvalidated(self) -> None:
        self._refresh_timer.start()
        # A replaced daemon or a changed Classic link can change what
        # tethering reports, and older daemons never send TetherChanged.
        self._tether_timer.start()

    @Slot()
    def _tetherInvalidated(self) -> None:
        self._tether_timer.start()

    @Slot(str)
    def _openMessageRequested(self, handle: str) -> None:
        self.messageOpenRequested.emit(handle)

    @Slot()
    def _callHistoryInvalidated(self) -> None:
        # Search hits may name calls that are gone.
        self._search.invalidate()
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
        self._call_history_limit = CALL_HISTORY_PAGE
        self._call_history_timer.stop()
        self._call_history_again = False
        if self._call_history or self._call_history_error:
            self._call_history = []
            self._call_history_rows = {}
            self._call_history_error = ""
            self.callHistoryChanged.emit()

    @Slot(str)
    def revealCall(self, call_id: str) -> None:
        """A search hit names this call: read every retained call unless
        the shown list already holds it (Search covers all of them)."""
        selected = str(call_id or "")
        if not selected or self._call_history_limit >= MAX_CALL_HISTORY_QUERY_LIMIT:
            return
        if any(selected in (row.get("ids") or [])
               for row in self._call_history_rows.get("all") or []):
            return
        self._call_history_limit = MAX_CALL_HISTORY_QUERY_LIMIT
        self.loadCallHistory()

    @Slot()
    def loadCallHistory(self) -> None:
        """Fetch the opt-in call list; never touches the conversation error."""
        if not self.callHistoryEnabled or not self._call_history_watched:
            return
        if self._call_history_loading:
            self._call_history_again = True
            return
        self._call_history_loading = True
        limit = self._call_history_limit

        def operation() -> tuple[list[dict], dict]:
            entries = self._backend.call_history(limit)
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
                self._tether_timer.start()
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
                self._set_error((
                    ReplyDisposition.STALE_SENDER if self._confirming_sender
                    else ReplyDisposition.STALE_GROUP
                ).message)
                return
        plan = state.plan_reply(
            body, thread_key=key, confirm_group=confirm_group,
            expected_group_token=pending[2] if confirm_group else None,
        )
        thread = plan.thread
        if plan.disposition is ReplyDisposition.NEEDS_PARTICIPANTS and thread is not None:
            self.groupParticipantsRequested.emit(thread.key, draft)
            return
        if plan.disposition is ReplyDisposition.CONFIRM_GROUP and thread is not None:
            self._pending_group = (key, plan.body, plan.expected_group_token)
            self._confirming_sender = False
            self.groupConfirmationRequested.emit(key, draft, "\n".join(thread.recipients))
            return
        if plan.disposition is ReplyDisposition.CONFIRM_SENDER and thread is not None:
            self._pending_group = (key, plan.body, plan.expected_group_token)
            self._confirming_sender = True
            self.senderConfirmationRequested.emit(
                key, draft, thread.name, ", ".join(thread.recipients),
            )
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

    @Slot(str, "QVariantList", str)
    def setGroupParticipants(self, key: str, recipients, draft: str) -> None:
        """Save a group's members; a ``draft`` held back for them is then sent."""
        selected = [str(value).strip() for value in recipients if str(value).strip()]
        if not key or not selected:
            return

        def completed(value: object) -> None:
            if isinstance(value, Thread):
                # Plan the held reply against the saved members, not the
                # snapshot that asked for them.
                self._state.group_participants_saved(value)
                self._threads = [thread.to_dict() for thread in self._state.threads]
                self.threadsChanged.emit()
                if value.reply_ready and draft.strip():
                    self.sendThread(value.key, draft, False)
            self.refresh()

        self._run(lambda: self._backend.set_group_participants(key, selected), completed)

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
            self._contact_results = self._state.contact_result_rows()
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
        calls = [call.to_dict() for call in snapshot.calls]
        audio = snapshot.audio.to_dict() if snapshot.audio is not None else {}
        if (
            calls == self._phone_calls and snapshot.state == self._calls_state
            and audio == self._phone_call_audio
        ):
            return  # an unchanged list must not rebuild the call cards
        if calls and not self._phone_calls and audio:
            self.loadCallAudioDevices()  # a call started: offer its switch
        self._phone_calls = calls
        self._calls_state = snapshot.state
        self._phone_call_audio = audio
        self._keypad.follow(phone_link_view.active_call_id(calls))
        self.phoneCallsChanged.emit()

    def _calls_unavailable(self, _message: str = "") -> None:
        # Status explains a disabled or missing feature; an in-flight list
        # must not leave stale calls or a stale "ready" state on screen.
        state = str(self._status.get("calls_state") or "disabled")
        if state == "ready":
            state = "unavailable"  # ListCalls just failed despite the status
        if self._phone_calls or state != self._calls_state or self._phone_call_audio:
            self._phone_calls = []
            self._calls_state = state
            self._phone_call_audio = {}
            self._keypad.follow("")
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

    @Slot(str, str)
    def sendCallTones(self, call_id: str, tones: str) -> None:
        """Keypad tones (DTMF) from the tone field; clears the draft."""
        if self._keypad.press(str(call_id or ""), str(tones or "")):
            self.setCallTonesDraft("")

    @Slot(str, result=bool)
    def pressCallTone(self, key: str) -> bool:
        """A keypad click or key during an active call: send it at once."""
        return self._keypad.press(self.activeCallId, str(key or ""))

    def _send_tones(self, call_id: str, tones: str, done, failed) -> None:
        self._run(
            lambda: self._backend.send_call_tones(call_id, tones), done, failed, busy=False,
        )

    @Slot()
    def loadCallAudioDevices(self) -> None:
        """Read the speakers and microphones (settings opened, a call started)."""
        if not call_audio_view.supported(self._status) or self._call_audio_loading:
            return
        self._call_audio_loading = True

        def completed(value: object) -> None:
            self._call_audio_loading = False
            devices = dict(value) if isinstance(value, dict) else {}
            if devices != self._call_audio_devices:
                self._call_audio_devices = devices
                self.callAudioDevicesChanged.emit()

        def failed(_message: str) -> None:
            # The lists stay as they were; "Same as system" is always there.
            self._call_audio_loading = False

        self._run(self._backend.call_audio_devices, completed, failed, busy=False)

    @Slot(str, str)
    def setCallAudioDevice(self, direction: str, name: str) -> None:
        """Save the speaker or microphone for every call ("" = system)."""
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_call_audio_device(str(direction), str(name or "")),
            completed,
        )

    @Slot(str, str)
    def setCallAudioDeviceForCall(self, direction: str, name: str) -> None:
        """Switch the running call to another speaker or microphone."""
        self._call_action(
            lambda: self._backend.set_call_audio_device_for_call(str(direction), str(name or ""))
        )

    @Slot()
    def syncContacts(self) -> None:
        self._run(self._backend.sync_contacts, lambda _value: self.refresh())

    @Slot()
    def restartBackend(self) -> None:
        def restarted(_value: object) -> None:
            self._features = features_view.with_notice(self._features, "")
            QTimer.singleShot(features_view.RESTART_REFRESH_DELAY_MS, self.refresh)
            QTimer.singleShot(features_view.RESTART_FEATURES_DELAY_MS, self.loadFeatures)

        self._run(restart_backend, restarted)

    # ---- settings: feature switches and the doctor report ------------------

    @Property("QVariantMap", notify=featuresChanged)
    def features(self):
        return features_view.view(self._features)

    @Slot()
    def loadFeatures(self) -> None:
        if self._feature_saving:
            return
        self._features_revision += 1
        revision = self._features_revision

        def done(value: object) -> None:
            if revision != self._features_revision:
                return
            self._features = features_view.loaded(self._features, value)
            self.featuresChanged.emit()

        def failed(message: str) -> None:
            if revision != self._features_revision:
                return
            # Older daemons have no GetFeatures; the UI falls back to hints.
            self._features = features_view.unavailable(self._features, message)
            self.featuresChanged.emit()

        self._run(lambda: self._backend.features(), done, failed, busy=False)

    @Slot(str, bool)
    def setFeature(self, name: str, enabled: bool) -> None:
        if name in self._feature_saving:
            return
        self._features_revision += 1
        self._feature_saving.add(name)
        # Show the choice while it is saved; a failure puts the old state back.
        previous = self._features
        self._features = features_view.assume(previous, str(name), bool(enabled))
        self.featuresChanged.emit()

        def done(value: object) -> None:
            self._feature_saving.discard(name)
            self._features = features_view.with_notice(
                self._features, features_view.set_notice(value))
            self.featuresChanged.emit()
            self.loadFeatures()

        def failed(message: str) -> None:
            self._feature_saving.discard(name)
            self._features = features_view.restore(self._features, previous, name)
            self.featuresChanged.emit()
            self._operation_failed(message)
            self.loadFeatures()

        self._run(lambda: self._backend.set_feature(str(name), bool(enabled)), done, failed)

    @Property("QVariantMap", notify=doctorChanged)
    def doctor(self):
        return doctor_report.view(self._doctor)

    def stop_doctor(self) -> None:
        """End the owned check when the application event loop has stopped."""
        self._doctor_process.stop()

    @Slot()
    def runDoctor(self) -> None:
        if self._doctor["running"]:
            return
        self._doctor = doctor_report.running_state(self._doctor)
        self.doctorChanged.emit()
        task = Task(self._doctor_runner)
        self._tasks.add(task)

        def done(report: object) -> None:
            self._doctor = doctor_report.finished_state(self._doctor, report)
            self.doctorChanged.emit()

        def failed(message: str) -> None:
            self._doctor = doctor_report.failed_state(message)
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

    # ---- opt-in tethering --------------------------------------------------

    def _apply_tether(self, value: object) -> None:
        if not isinstance(value, TetherStatus):
            return
        self._tether = {
            "available": True,
            "pending": self._tether_pending,
            "state": value.state,
            "interface": value.interface,
            "backend": value.backend,
            "external": value.external,
            "error": value.error,
            "needs_dhcp": value.needs_dhcp,
            "enabled": value.enabled,
            "autoconnect": value.autoconnect,
            "active": value.active,
            "summary": value.summary(),
        }
        self.tetherChanged.emit()

    def _tether_result(self, value: object) -> None:
        if value is None:
            # The running daemon has no Tether1 (an older release): hide the
            # control. Any other failure keeps the last known state.
            self._tether = {"available": False}
            self.tetherChanged.emit()
            return
        self._apply_tether(value)

    def _tether_failed(self, message: str) -> None:
        self._operation_failed(message)

    def _tether_request(self, request: Callable[[], object]) -> Callable[[], object]:
        def run() -> object:
            try:
                return request()
            except TetherUnsupportedError:
                return None
        return run

    @Slot()
    def refreshTether(self) -> None:
        self._run(
            self._tether_request(lambda: self._backend.tether_state()),
            self._tether_result,
            self._tether_failed,
            busy=False,
        )

    def _tether_command(self, request: Callable[[], object]) -> None:
        if self._tether_pending:
            return
        self._tether_pending = True
        self._tether = {**self._tether, "pending": True}
        self.tetherChanged.emit()

        def completed(value: object) -> None:
            self._tether_pending = False
            self._tether_result(value)

        def failed(message: str) -> None:
            self._tether_pending = False
            self._tether = {**self._tether, "pending": False}
            self.tetherChanged.emit()
            self._tether_failed(message)
            self._tether_timer.start()

        self._run(self._tether_request(request), completed, failed, busy=False)

    @Slot(bool)
    def setTetherConnected(self, connected: bool) -> None:
        """Explicit user action; the daemon never tethers without one."""
        def request() -> object:
            if connected:
                return self._backend.tether_connect()
            return self._backend.tether_disconnect()

        self._tether_command(request)

    @Slot(bool, bool)
    def setTethering(self, enabled: bool, autoconnect: bool) -> None:
        """Save the opt-in; off makes the daemon ignore PAN links entirely."""
        self._tether_command(
            lambda: self._backend.tether_configure(bool(enabled), bool(autoconnect))
        )

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

    @Slot()
    def resetStorage(self) -> None:
        # The reset obtains its own key, so skip the automatic unlock.
        self._storage_unlock_attempted = True

        self._run(self._backend.reset_storage, self._storage_updated)

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


def _file_url(path: object) -> str:
    """A local path as the file URL QML shows ("" for none)."""
    from PySide6.QtCore import QUrl

    return QUrl.fromLocalFile(str(path)).toString() if path else ""


def _open_file(path: object) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
