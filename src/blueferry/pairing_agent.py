"""Short-lived BlueZ pairing agent used by interactive setup clients."""

from __future__ import annotations

import threading
import time
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

    def _confirm_deferred(
        self,
        passkey: int | None,
        rejection: str,
        return_cb: Callable[[], None],
        error_cb: Callable[[Exception], None],
    ) -> None:
        # BlueZ expects an answer within its own pairing timeout, but the
        # confirmation callback may wait on a human. Blocking this dispatch
        # would also block the GLib loop that carries the rest of the
        # transaction, so answer through a deferred D-Bus reply instead.
        def confirm() -> None:
            try:
                if self._confirmation(passkey):
                    return_cb()
                else:
                    error_cb(_Rejected(rejection))
            except Exception as error:  # callback/UI failures reject securely
                error_cb(error)

        threading.Thread(
            target=confirm,
            name="blueferry-pairing-confirmation",
            daemon=True,
        ).start()

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

    @dbus.service.method(
        AGENT_INTERFACE,
        in_signature="ou",
        out_signature="",
        async_callbacks=("return_cb", "error_cb"),
    )
    def RequestConfirmation(
        self,
        device: dbus.ObjectPath,
        passkey: dbus.UInt32,
        return_cb: Callable[[], None],
        error_cb: Callable[[Exception], None],
    ) -> None:
        self._require_expected(device)
        self._confirm_deferred(
            int(passkey),
            "Pairing code was not confirmed",
            return_cb,
            error_cb,
        )

    @dbus.service.method(
        AGENT_INTERFACE,
        in_signature="o",
        out_signature="",
        async_callbacks=("return_cb", "error_cb"),
    )
    def RequestAuthorization(
        self,
        device: dbus.ObjectPath,
        return_cb: Callable[[], None],
        error_cb: Callable[[Exception], None],
    ) -> None:
        self._require_expected(device)
        self._confirm_deferred(
            None,
            "Pairing was not authorized",
            return_cb,
            error_cb,
        )

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: dbus.ObjectPath, uuid: str) -> None:
        self._require_expected(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        pass


class RegisteredPairingAgent:
    """Register a same-thread agent for one interactive pairing transaction.

    The agent's D-Bus callbacks are dispatched by a GLib loop running on the
    caller's thread (``wait_for_pair``). An earlier design ran the loop on a
    background thread next to a synchronous ``Device1.Pair()``; dbus-python
    intermittently lost the confirmation reply in that arrangement, producing
    spurious authentication failures.
    """

    def __init__(
        self,
        expected_device: str,
        confirmation: ConfirmationCallback,
        display: DisplayCallback | None = None,
    ) -> None:
        self._bus = get_system_bus()
        self._expected_device = expected_device
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

    def __enter__(self) -> RegisteredPairingAgent:
        try:
            self._manager.RegisterAgent(
                dbus.ObjectPath(AGENT_PATH),
                "DisplayYesNo",
                timeout=10.0,
            )
            self._registered = True
            # Own incoming requests the way desktop pairing managers do, so
            # an iPhone-initiated transaction reaches this confirmation UI
            # instead of racing whichever desktop agent happens to exist.
            self._manager.RequestDefaultAgent(
                dbus.ObjectPath(AGENT_PATH),
                timeout=10.0,
            )
        except dbus.exceptions.DBusException as error:
            self._unregister()
            self._agent.remove_from_connection()
            detail = error.get_dbus_message() or error.get_dbus_name() or str(error)
            raise PairingError(f"Could not start secure pairing confirmation: {detail}") from error
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self._unregister()
        self._agent.remove_from_connection()

    def _unregister(self) -> None:
        if not self._registered:
            return
        try:
            self._manager.UnregisterAgent(
                dbus.ObjectPath(AGENT_PATH),
                timeout=10.0,
            )
        except dbus.exceptions.DBusException:
            pass
        self._registered = False

    def wait_for_pair(self, timeout: float = 120.0) -> None:
        """Dispatch Agent1 callbacks until BlueZ reports the device paired."""
        loop = GLib.MainLoop()
        deadline = time.monotonic() + timeout
        failure: list[PairingError] = []
        props = dbus.Interface(
            self._bus.get_object("org.bluez", self._expected_device),
            "org.freedesktop.DBus.Properties",
        )

        def check_state() -> bool:
            try:
                paired = bool(props.Get("org.bluez.Device1", "Paired"))
            except dbus.exceptions.DBusException as error:
                # BlueZ can briefly delete and recreate the device object
                # during an incoming transaction.
                if error.get_dbus_name() == "org.freedesktop.DBus.Error.UnknownObject":
                    paired = False
                else:
                    failure.append(
                        PairingError(f"Could not inspect pairing state: {error}")
                    )
                    loop.quit()
                    return GLib.SOURCE_REMOVE
            if paired:
                loop.quit()
                return GLib.SOURCE_REMOVE
            if time.monotonic() >= deadline:
                failure.append(PairingError(
                    "Timed out waiting for the iPhone to pair. Keep Bluetooth "
                    "settings open on the phone and tap this computer's name."
                ))
                loop.quit()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(250, check_state)
        loop.run()
        if failure:
            raise failure[0]
