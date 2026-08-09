"""Asynchronous presentation controller for the Kirigami client."""
from __future__ import annotations

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
from blueferry.client import BackendClient, BackendError
from blueferry.i18n import _
from blueferry.onboarding import derive_stage
from blueferry.protocol import BUS_NAME, EVENTS_IFACE, OBJECT_PATH
from blueferry.qt.tasks import Task
from blueferry.setup_client import SetupClient


class BridgeController(QObject):
    threadsChanged = Signal()
    statusChanged = Signal()
    devicesChanged = Signal()
    bluetoothChanged = Signal()
    busyChanged = Signal()
    errorTextChanged = Signal()
    compatibilityChanged = Signal()
    configuredChanged = Signal()
    setupLoadedChanged = Signal()
    onboardingStageChanged = Signal()

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
        self._status: dict = {}
        self._devices: list[dict] = []
        self._bluetooth_active = False
        self._busy_count = 0
        self._error_text = ""
        self._compatibility: dict = {}
        self._configured = False
        self._setup_loaded = False
        self._onboarding_stage = str(derive_stage(
            setup_loaded=self._setup_loaded,
            configured=self._configured,
            compatibility=self._compatibility,
            status=self._status,
        ))
        self._refreshing = False
        self._refresh_again = False
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

    @Property("QVariantMap", notify=compatibilityChanged)
    def compatibility(self):
        return self._compatibility

    @Property(bool, notify=configuredChanged)
    def configured(self) -> bool:
        return self._configured

    @Property(bool, notify=setupLoadedChanged)
    def setupLoaded(self) -> bool:
        return self._setup_loaded

    @Property(str, notify=onboardingStageChanged)
    def onboardingStage(self) -> str:
        return self._onboarding_stage

    def _update_onboarding_stage(self) -> None:
        stage = str(derive_stage(
            setup_loaded=self._setup_loaded,
            configured=self._configured,
            compatibility=self._compatibility,
            status=self._status,
        ))
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

    @Slot("QVariantMap")
    def _historyChanged(self, _revision) -> None:
        self._refresh_timer.start()

    @Slot()
    def _statusInvalidated(self) -> None:
        self._refresh_timer.start()

    @Slot()
    def start(self) -> None:
        def initialize():
            configuration = self._setup.configuration()
            status = ensure_backend_current() if configuration.configured else {}
            return configuration, status

        def ready(result: object) -> None:
            configuration, status = result
            self._configured = configuration.configured
            self._setup_loaded = True
            self.configuredChanged.emit()
            self.setupLoadedChanged.emit()
            if status:
                self._status = dict(status)
                self.statusChanged.emit()
            self._set_error("")
            if self._configured:
                self.refresh()
            self.loadSetupState()
            self.loadDevices(False)
            self._update_onboarding_stage()

        def failed(message: str) -> None:
            self._set_error(
                _("Background service unavailable: {error}").format(error=message)
            )
            self._setup_loaded = True
            self.setupLoadedChanged.emit()
            self._update_onboarding_stage()
            self.loadSetupState()
            self.loadDevices(False)

        self._run(initialize, ready, failed)

    @Slot()
    def loadSetupState(self) -> None:
        def operation():
            return self._setup.compatibility(), self._setup.configuration()

        def completed(value: object) -> None:
            compatibility, configuration = value
            self._compatibility = compatibility.to_dict()
            self._configured = configuration.configured
            self._setup_loaded = True
            self._bluetooth_active = compatibility.bearer_api_active
            self.compatibilityChanged.emit()
            self.configuredChanged.emit()
            self.setupLoadedChanged.emit()
            self.bluetoothChanged.emit()
            self._update_onboarding_stage()
            if self._devices:
                self.loadDevices(False)

        self._run(operation, completed, busy=False)

    def _snapshot(self) -> dict:
        def safely(operation, fallback):
            try:
                return operation()
            except BackendError:
                return fallback

        status: dict
        try:
            status = self._backend.status().to_dict()
        except BackendError as error:
            status = {"daemon": False, "error": str(error)}
        return {
            "threads": safely(
                lambda: [item.to_dict() for item in self._backend.threads()], []
            ),
            "status": status,
        }

    @Slot()
    def refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True

        def completed(snapshot: object) -> None:
            value = snapshot if isinstance(snapshot, dict) else {}
            self._threads = list(value.get("threads", []))
            self._status = dict(value.get("status", {}))
            self.threadsChanged.emit()
            self.statusChanged.emit()
            self._update_onboarding_stage()
            if self._status.get("daemon"):
                self._set_error("")

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
            lambda: self._backend.send_to_thread(
                key, body, confirm_group=confirm_group
            ),
            completed,
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
    def setNotificationPolicy(self, policy: str) -> None:
        def completed(value: object) -> None:
            selected = str(value)
            self._status["notification_policy"] = selected
            self.statusChanged.emit()

        self._run(
            lambda: self._backend.set_notification_policy(policy), completed
        )

    @Slot(str)
    def setStoragePolicy(self, policy: str) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
            self.refresh()

        self._run(lambda: self._backend.set_storage_policy(policy), completed)

    @Slot()
    def unlockStorage(self) -> None:
        def completed(value: object) -> None:
            if isinstance(value, dict):
                self._status.update(value)
                self.statusChanged.emit()
            self.refresh()

        self._run(self._backend.unlock_storage, completed)

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
            matching = [
                item for item in devices
                if not adapter or item.adapter_path.endswith(f"/{adapter}")
            ]
            likely = [item for item in matching if item.likely_iphone]
            self._devices = []
            for item in likely or matching:
                value = item.to_dict()
                if item.paired:
                    value["display_name"] = _("{name} — paired").format(name=item.name)
                else:
                    value["display_name"] = item.name
                self._devices.append(value)
            self.devicesChanged.emit()
            if scan and not self._devices:
                self._set_error(_(
                    "No Bluetooth devices found; unlock the iPhone and keep "
                    "Bluetooth settings open"
                ))

        self._run(
            lambda: self._setup.devices(scan_seconds=8 if scan else 0),
            completed,
        )

    @Slot()
    def activateBluetooth(self) -> None:
        def completed(_value: object) -> None:
            self.loadSetupState()
            self.loadDevices(True)

        self._run(self._setup.activate_bluez, completed)

    @Slot(str)
    def completePairing(self, mac: str) -> None:
        def completed(_value: object) -> None:
            self._configured = True
            self.configuredChanged.emit()
            self._update_onboarding_stage()
            self.loadDevices(False)
            self.loadSetupState()
            self.refresh()

        self._run(lambda: self._setup.complete(mac), completed)

    @Slot(str)
    def forgetDevice(self, mac: str) -> None:
        def completed(_value: object) -> None:
            self.loadDevices(False)

        self._run(lambda: self._setup.forget(mac), completed)
