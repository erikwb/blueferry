"""Short-lived BlueZ pairing agent used by interactive setup clients."""

from __future__ import annotations

import threading
from collections.abc import Callable

import dbus
import dbus.exceptions
import dbus.service
from gi.repository import GLib

from blueferry.bus import get_system_bus
from blueferry.errors import PairingError

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/io/weirdware/BlueFerry/PairingAgent"

ConfirmationCallback = Callable[[int | None], bool]
DisplayCallback = Callable[[int], None]


class _Rejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class PairingAgent(dbus.service.Object):
    """Authorize only the device selected by this pairing transaction."""

    def __init__(
        self,
        bus: dbus.Bus,
        expected_device: str,
        confirmation: ConfirmationCallback,
        display: DisplayCallback | None = None,
    ) -> None:
        super().__init__(bus, AGENT_PATH)
        self._expected_device = expected_device
        self._confirmation = confirmation
        self._display = display

    def _require_expected(self, device: dbus.ObjectPath) -> None:
        if str(device) != self._expected_device:
            raise _Rejected("BlueFerry did not request pairing with this device")

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: dbus.ObjectPath) -> str:
        self._require_expected(device)
        raise _Rejected("PIN entry is not supported; use numeric comparison")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device: dbus.ObjectPath, pincode: str) -> None:
        self._require_expected(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: dbus.ObjectPath) -> dbus.UInt32:
        self._require_expected(device)
        raise _Rejected("Passkey entry is not supported; use numeric comparison")

    @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(
        self,
        device: dbus.ObjectPath,
        passkey: dbus.UInt32,
        entered: dbus.UInt16,
    ) -> None:
        self._require_expected(device)
        if self._display is not None:
            self._display(int(passkey))

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(
        self,
        device: dbus.ObjectPath,
        passkey: dbus.UInt32,
    ) -> None:
        self._require_expected(device)
        if not self._confirmation(int(passkey)):
            raise _Rejected("Pairing code was not confirmed")

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: dbus.ObjectPath) -> None:
        self._require_expected(device)
        if not self._confirmation(None):
            raise _Rejected("Pairing was not authorized")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: dbus.ObjectPath, uuid: str) -> None:
        self._require_expected(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        pass


class RegisteredPairingAgent:
    """Register an agent for one call to ``Device1.Pair`` and clean it up."""

    def __init__(
        self,
        expected_device: str,
        confirmation: ConfirmationCallback,
        display: DisplayCallback | None = None,
    ) -> None:
        self._bus = get_system_bus()
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(
            target=self._loop.run,
            name="blueferry-pairing-agent",
            daemon=True,
        )
        self._agent = PairingAgent(
            self._bus,
            expected_device,
            confirmation,
            display,
        )
        self._manager = dbus.Interface(
            self._bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.AgentManager1",
        )
        self._registered = False

    def __enter__(self) -> PairingAgent:
        self._thread.start()
        try:
            self._manager.RegisterAgent(
                dbus.ObjectPath(AGENT_PATH),
                "DisplayYesNo",
                timeout=10.0,
            )
        except dbus.exceptions.DBusException as error:
            self._agent.remove_from_connection()
            self._stop_loop()
            detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
            raise PairingError(f"Could not start secure pairing confirmation: {detail}") from error
        self._registered = True
        return self._agent

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._registered:
            try:
                self._manager.UnregisterAgent(
                    dbus.ObjectPath(AGENT_PATH),
                    timeout=10.0,
                )
            except dbus.exceptions.DBusException:
                pass
        self._agent.remove_from_connection()
        self._stop_loop()

    def _stop_loop(self) -> None:
        self._loop.quit()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
