"""Short-lived BlueZ pairing agent used by interactive setup clients."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import dbus
import dbus.exceptions
import dbus.service
from gi.repository import GLib

from blueferry.bus import get_system_bus
from blueferry.errors import PairingError

log = logging.getLogger(__name__)

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/io/weirdware/BlueFerry/PairingAgent"
_BLUETOOTH_BASE_UUID = "0000{:04x}-0000-1000-8000-00805f9b34fb"
_AUTHORIZED_SERVICE_UUIDS = frozenset({
    _BLUETOOTH_BASE_UUID.format(code)
    for code in (
        0x112E,  # Phonebook Access client
        0x112F,  # Phonebook Access server
        0x1132,  # Message Access server
        0x1133,  # Message Notification server
        0x1134,  # Message Access client
    )
} | {
    "7905f431-b5ce-4e99-a40f-4b1e122d00d0",  # Apple Notification Center Service
})

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
            log.warning(
                "rejecting pairing-agent request for unexpected device %s",
                device,
            )
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
                    log.debug("pairing-agent confirmation accepted")
                    return_cb()
                else:
                    log.debug("pairing-agent confirmation rejected")
                    error_cb(_Rejected(rejection))
            except Exception as error:  # callback/UI failures reject securely
                log.debug("pairing-agent confirmation callback failed", exc_info=True)
                error_cb(error)

        threading.Thread(
            target=confirm,
            name="blueferry-pairing-confirmation",
            daemon=True,
        ).start()

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        log.debug("BlueZ released the BlueFerry pairing agent")

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: dbus.ObjectPath) -> str:
        self._require_expected(device)
        raise _Rejected("PIN entry is not supported; use numeric comparison")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device: dbus.ObjectPath, pincode: str) -> None:
        self._require_expected(device)
        raise _Rejected("PIN display is not supported; use numeric comparison")

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
        log.debug("BlueZ requested numeric confirmation for %s", device)
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
        log.debug("BlueZ requested pairing authorization for %s", device)
        self._confirm_deferred(
            None,
            "Pairing was not authorized",
            return_cb,
            error_cb,
        )

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: dbus.ObjectPath, uuid: str) -> None:
        self._require_expected(device)
        normalized = str(uuid).strip().casefold()
        if normalized not in _AUTHORIZED_SERVICE_UUIDS:
            log.warning(
                "rejecting unexpected Bluetooth service %s for %s",
                uuid,
                device,
            )
            raise _Rejected("Bluetooth service is outside BlueFerry's profile allowlist")
        log.debug("authorizing BlueFerry Bluetooth service %s for %s", uuid, device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        log.debug("BlueZ cancelled the pairing-agent request")


class RegisteredPairingAgent:
    """Register a device-scoped agent for one interactive transaction.

    iPhone-initiated pairing makes this agent temporarily default so the
    authentication request reaches it. Explicit ``Device1.Pair`` instead
    selects the agent through its D-Bus connection. Both paths dispatch
    callbacks on this thread's GLib loop.
    """

    def __init__(
        self,
        expected_device: str,
        confirmation: ConfirmationCallback,
        display: DisplayCallback | None = None,
        *,
        make_default: bool = False,
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
        self._make_default = make_default

    def __enter__(self) -> RegisteredPairingAgent:
        try:
            log.info(
                "registering device-scoped pairing agent for %s",
                self._expected_device,
            )
            self._manager.RegisterAgent(
                dbus.ObjectPath(AGENT_PATH),
                "DisplayYesNo",
                timeout=10.0,
            )
            self._registered = True
            if self._make_default:
                # The transaction is initiated by the iPhone, so BlueZ must
                # route its confirmation to this scoped agent.
                # UnregisterAgent restores the previous default afterward.
                self._manager.RequestDefaultAgent(
                    dbus.ObjectPath(AGENT_PATH),
                    timeout=10.0,
                )
                log.info("BlueFerry pairing agent is now the BlueZ default")
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
            log.info(
                "unregistering BlueFerry pairing agent%s",
                "; restoring previous default" if self._make_default else "",
            )
            self._manager.UnregisterAgent(
                dbus.ObjectPath(AGENT_PATH),
                timeout=10.0,
            )
        except dbus.exceptions.DBusException as error:
            log.debug(
                "could not unregister pairing agent: %s",
                error.get_dbus_name() or str(error),
            )
        self._registered = False

    def pair(self, timeout: float = 120.0) -> None:
        """Run explicit Device1.Pair while dispatching Agent1 callbacks."""
        log.info("sending Device1.Pair for %s", self._expected_device)
        loop = GLib.MainLoop()
        failure: list[Exception] = []
        finished = False

        def paired() -> None:
            nonlocal finished
            finished = True
            log.info("Device1.Pair completed successfully")
            loop.quit()

        def failed(error: Exception) -> None:
            nonlocal finished
            finished = True
            failure.append(error)
            loop.quit()

        def timed_out() -> bool:
            if finished:
                return GLib.SOURCE_REMOVE
            failure.append(PairingError(
                "Timed out waiting for the iPhone to pair. Keep Bluetooth "
                "settings open on the phone and retry."
            ))
            loop.quit()
            return GLib.SOURCE_REMOVE

        interface = dbus.Interface(
            self._bus.get_object("org.bluez", self._expected_device),
            "org.bluez.Device1",
        )
        interface.Pair(
            reply_handler=paired,
            error_handler=failed,
            timeout=timeout,
        )
        timeout_source = GLib.timeout_add(int(timeout * 1000), timed_out)
        try:
            loop.run()
        finally:
            if finished:
                GLib.source_remove(timeout_source)
        if failure:
            raise failure[0]

    def wait_for_pair(self, timeout: float = 120.0) -> None:
        """Dispatch Agent1 callbacks until BlueZ reports the device paired."""
        log.debug(
            "waiting up to %.0fs for Device1.Paired on %s",
            timeout,
            self._expected_device,
        )
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
                log.info("BlueZ reports the selected device paired")
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
