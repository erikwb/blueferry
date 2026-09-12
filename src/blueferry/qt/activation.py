"""Qt event-loop adapter for the shared desktop activation protocol."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage

from blueferry.client_activation import ACTIVATION_INTERFACE, ACTIVATION_PATH, CLIENTS

QT_CLIENT = next(client for client in CLIENTS if client.key == "qt")


class ClientActivation(QObject):
    requested = Signal(str, str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._ready = False
        self._pending: tuple[str, str] | None = None
        self.bus = QDBusConnection.sessionBus()
        self.primary = self.bus.registerService(QT_CLIENT.bus_name)
        if self.primary and not self.bus.registerObject(
            ACTIVATION_PATH, ACTIVATION_INTERFACE, self,
            QDBusConnection.RegisterOption.ExportAllSlots,
        ):
            self.bus.unregisterService(QT_CLIENT.bus_name)
            raise RuntimeError("could not register desktop activation")

    @Slot(str, str)
    def OpenMessage(self, handle: str, token: str) -> None:
        if len(handle) <= 1024 and len(token) <= 4096:
            if self._ready:
                self.requested.emit(handle, token)
            else:
                self._pending = (handle, token)

    def ready(self) -> None:
        """QML loading can dispatch D-Bus before a window is available."""
        self._ready = True
        if self._pending is not None:
            request, self._pending = self._pending, None
            self.requested.emit(*request)

    def forward(self, handle: str, token: str) -> bool:
        message = QDBusMessage.createMethodCall(
            QT_CLIENT.bus_name, ACTIVATION_PATH, ACTIVATION_INTERFACE, "OpenMessage",
        )
        message.setArguments([handle, token])
        reply = self.bus.call(message, timeout=3000)
        return reply.type() != QDBusMessage.MessageType.ErrorMessage

    def close(self) -> None:
        if self.primary:
            self.bus.unregisterObject(ACTIVATION_PATH)
            self.bus.unregisterService(QT_CLIENT.bus_name)
