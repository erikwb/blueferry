"""Asynchronous presentation controller for the Kirigami client."""

from __future__ import annotations

import threading
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
from blueferry.client import BackendClient, BackendError
from blueferry.i18n import _
from blueferry.models import BackendStatus
from blueferry.onboarding import OnboardingState, effective_compatibility
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH
from blueferry.qt.tasks import Task
from blueferry.quirks_report import issue_report, issue_url
from blueferry.setup_client import (
    DISCOVERY_SECONDS,
    ConfigurationState,
    SetupClient,
)


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
        self._contact_results: list[dict] = []
        self._contact_query = ""
        self._status: dict = {}
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
        self._storage_unlock_attempted = False
        self._pairing_confirmation_lock = threading.Lock()
        self._pairing_confirmation: tuple[threading.Event, list[bool]] | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
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

    @Property("QVariantMap", notify=statusChanged)
    def status(self):
        return self._status

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

    @Slot("QVariantMap")
    def _historyChanged(self, _revision) -> None:
        self._refresh_timer.start()

    @Slot()
    def _statusInvalidated(self) -> None:
        self._refresh_timer.start()

    @Slot(str)
    def _openMessageRequested(self, handle: str) -> None:
        self.messageOpenRequested.emit(handle)

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
                self.statusChanged.emit()
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
            self._bluetooth_active = compatibility.bearer_api_active
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
            self._bluetooth_active = bool(getattr(compatibility, "bearer_api_active", False))
            self.compatibilityChanged.emit()
            self.bluetoothChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)

        def failed(message: str) -> None:
            self._set_compatibility_unavailable(selected, message)
            self.loadDevices(False)

        self._run(lambda: self._setup.compatibility(selected), completed, failed)

    def _snapshot(self) -> dict:
        status: dict
        try:
            status = self._backend.status().to_dict()
        except BackendError as error:
            status = {"daemon": False, "error": str(error)}
        threads: list[dict] | None
        thread_error = ""
        try:
            threads = [item.to_dict() for item in self._backend.threads()]
        except BackendError as error:
            # A failed snapshot is not evidence that history is empty. Keep
            # the last successful projection visible and report the transient
            # failure separately.
            threads = None
            thread_error = str(error)
        return {
            "threads": threads,
            "status": status,
            "thread_error": thread_error,
        }

    def _apply_snapshot(self, snapshot: object) -> None:
        value = snapshot if isinstance(snapshot, dict) else {}
        threads = value.get("threads")
        if isinstance(threads, list):
            self._threads = list(threads)
            self.threadsChanged.emit()
        status = value.get("status")
        if isinstance(status, dict):
            self._status = dict(status)
            self.statusChanged.emit()
            self._maybe_unlock_storage()
        self._update_onboarding_stage()
        self._refresh_pairing_issue_report()
        thread_error = str(value.get("thread_error") or "")
        if thread_error:
            self._set_error(thread_error)
        elif self._status.get("daemon"):
            self._set_error("")

    @Slot()
    def refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True

        def completed(snapshot: object) -> None:
            self._apply_snapshot(snapshot)

        def failed(message: str) -> None:
            self._set_error(message)

        def finished() -> None:
            self._refreshing = False
            if self._refresh_again:
                self._refresh_again = False
                self.refresh()

        task = Task(self._snapshot)
        self._tasks.add(task)
        task.signals.done.connect(completed)
        task.signals.failed.connect(failed)
        task.signals.finished.connect(lambda: (self._tasks.discard(task), finished()))
        self._pool.start(task)

    @Slot(str, str, bool)
    def sendThread(self, key: str, body: str, confirm_group: bool) -> None:
        body = body.strip()
        if not key or not body:
            return

        def completed(_value: object) -> None:
            self.refresh()

        self._run(
            lambda: self._backend.send_to_thread(key, body, confirm_group=confirm_group),
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
        selected = query.strip()
        self._contact_query = selected
        if not selected:
            self._contact_results = []
            self.contactResultsChanged.emit()
            return

        def completed(value: object) -> None:
            if self._contact_query != selected:
                return
            matches = list(value) if isinstance(value, list) else []
            self._contact_results = [
                {"name": str(name), "address": str(address)}
                for name, address in matches
            ]
            self.contactResultsChanged.emit()

        self._run(
            lambda: self._backend.find_contacts(selected),
            completed,
            busy=False,
        )

    @Slot(str, str)
    def sendMessage(self, recipient: str, body: str) -> None:
        recipient = recipient.strip()
        body = body.strip()
        if not recipient or not body:
            return

        self._run(
            lambda: self._backend.send(recipient, body),
            lambda _value: self.refresh(),
        )

    @Slot()
    def syncContacts(self) -> None:
        def completed(_value: object) -> None:
            self.refresh()

        self._run(self._backend.sync_contacts, completed)

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
            selected = str(value)
            self._status["notification_policy"] = selected
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

    @Slot(str)
    def setStoragePolicy(self, policy: str) -> None:
        if policy == "encrypted":
            # SetStoragePolicy already opens the wallet when encryption is
            # selected, so do not immediately issue a duplicate request.
            self._storage_unlock_attempted = True

        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
            self.refresh()

        self._run(lambda: self._backend.set_storage_policy(policy), completed)

    @Slot()
    def unlockStorage(self) -> None:
        self._storage_unlock_attempted = True

        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
            self.refresh()

        self._run(self._backend.unlock_storage, completed)

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
            self.configuredChanged.emit()
            self.compatibilityChanged.emit()
            self.statusChanged.emit()
            self.threadsChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)
            self.loadSetupState()

        adapter = self._configuration.adapter.strip() or None
        self._run(lambda: self._setup.forget(mac, adapter=adapter), completed)
