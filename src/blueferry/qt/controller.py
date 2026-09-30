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

from blueferry import __version__
from blueferry.backend_lifecycle import ensure_backend_current, restart_backend
from blueferry.bluetooth_devices import iphone_candidates
from blueferry.client import BackendClient, TetherUnsupportedError
from blueferry.conversation_state import (
    ConversationSnapshot,
    ConversationState,
    ReplyDisposition,
    fetch_conversation_snapshot,
)
from blueferry.i18n import _
from blueferry.models import BackendStatus, CallsSnapshot
from blueferry.onboarding import OnboardingState, effective_compatibility
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH, TETHER_IFACE
from blueferry.qt.avatars import avatar_url
from blueferry.qt.tasks import Task
from blueferry.quirks_report import issue_report, issue_url
from blueferry.service_manager import bluetooth_restart_command
from blueferry.setup_client import (
    DISCOVERY_SECONDS,
    ConfigurationState,
    SetupClient,
)
from blueferry.tether_status import TetherStatus

# Bounds on the per-window avatar cache (least recently used entries are
# dropped). The count covers a long list of small photos; the byte bound
# keeps large ones (up to limits.MAX_CONTACT_PHOTO_BYTES each) from holding
# hundreds of MiB of encoded images.
MAX_CACHED_AVATARS = 512
MAX_CACHED_AVATAR_BYTES = 32 * 1024 * 1024
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
    phoneCallsChanged = Signal()
    tetherChanged = Signal()

    def __init__(
        self,
        backend: BackendClient | None = None,
        setup: SetupClient | None = None,
        *,
        subscribe: bool = True,
        autostart: bool = True,
        parent: QObject | None = None,
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
        self._bluetooth_active = False
        self._busy_count = 0
        self._error_text = ""
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
        self._tether: dict = {"available": False}
        self._tether_timer = QTimer(self)
        self._tether_timer.setSingleShot(True)
        self._tether_timer.setInterval(100)
        self._tether_timer.timeout.connect(self.refreshTether)
        self._bus = QDBusConnection.sessionBus() if subscribe else None
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
                return
            self._avatar_failures.pop(key, None)
            data = value if isinstance(value, bytes) and value else None
            if data is None:
                self._avatar_missing[key] = None
                while len(self._avatar_missing) > MAX_CACHED_AVATARS:
                    self._avatar_missing.popitem(last=False)
                return
            with self._avatar_lock:
                self._avatars[key] = data
                cached = sum(len(item) for item in self._avatars.values())
                # The newest avatar always stays, even above the byte bound.
                while len(self._avatars) > 1 and (
                    len(self._avatars) > MAX_CACHED_AVATARS
                    or cached > MAX_CACHED_AVATAR_BYTES
                ):
                    _old, dropped = self._avatars.popitem(last=False)
                    cached -= len(dropped)
            self._avatar_revision += 1
            self.avatarsChanged.emit()

        def failed(_message: str) -> None:
            # Avatars are decoration: never surface a lookup, busy-store, or
            # rate-limit failure as an error banner. Retry after a backoff.
            self._avatar_pending.discard(key)
            if generation != self._avatar_generation:
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

        self._run(lambda: self._backend.contact_photo(key), fetched, failed, busy=False)
        return ""

    def _schedule_avatar_retry(self, delay_ms: int, key: str, generation: int) -> None:
        def release() -> None:
            if generation != self._avatar_generation:
                return
            self._avatar_backoff.pop(key, None)
            # Let QML ask again; the binding re-reads avatarSource().
            self._avatar_revision += 1
            self.avatarsChanged.emit()

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
        self._avatar_revision += 1
        self.avatarsChanged.emit()

    @Property(bool, notify=statusChanged)
    def callHistoryEnabled(self) -> bool:
        return self._status.get("call_history_enabled") is True

    @Property("QVariantList", notify=callHistoryChanged)
    def callHistory(self):
        return self._call_history

    @Property(str, notify=callHistoryChanged)
    def callHistoryError(self) -> str:
        return self._call_history_error
    @Property("QVariantList", notify=notificationOpenMapChanged)
    def notificationOpenMap(self):
        return self._notification_open_map

    @Property("QVariantMap", notify=nowPlayingChanged)
    def nowPlaying(self):
        return self._now_playing
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

    @Slot("QVariantMap")
    def _historyChanged(self, _revision) -> None:
        self._refresh_timer.start()

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

        def operation() -> list[dict]:
            return [entry.to_dict() for entry in self._backend.call_history(200)]

        def completed(value: object) -> None:
            # A reply that lands after the page closed is discarded.
            if self._call_history_watched:
                self._call_history = list(value) if isinstance(value, list) else []
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
    def syncContacts(self) -> None:
        self._run(self._backend.sync_contacts, lambda _value: self.refresh())

    @Slot()
    def restartBackend(self) -> None:
        self._run(
            restart_backend,
            lambda _value: QTimer.singleShot(800, self.refresh),
        )

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

    def _apply_tether(self, value: object, *, pending: bool = False) -> None:
        if not isinstance(value, TetherStatus):
            return
        self._tether = {
            "available": True,
            "pending": pending,
            "state": value.state,
            "interface": value.interface,
            "backend": value.backend,
            "external": value.external,
            "error": value.error,
            "needs_dhcp": value.needs_dhcp,
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
        if self._tether.get("pending") is True:
            self._tether = {**self._tether, "pending": False}
            self.tetherChanged.emit()
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

    @Slot(bool)
    def setTetherEnabled(self, enabled: bool) -> None:
        """Explicit user action; the daemon never tethers without one."""
        if self._tether.get("pending") is True:
            return
        self._tether = {**self._tether, "pending": True}
        self.tetherChanged.emit()
        def request() -> object:
            if enabled:
                return self._backend.tether_connect()
            return self._backend.tether_disconnect()

        def failed(message: str) -> None:
            self._tether_failed(message)
            self._tether_timer.start()

        self._run(self._tether_request(request), self._tether_result, failed, busy=False)

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
