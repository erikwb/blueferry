"""Opt-in internet tethering through the iPhone's Personal Hotspot.

The iPhone offers its Personal Hotspot over Bluetooth PAN in the NAP role.
BlueZ exposes that as ``org.bluez.Network1`` on the paired device, and
``Network1.Connect("nap")`` returns the kernel ``bnep`` interface. IP
configuration is a separate step: NetworkManager can own both steps, and
without it BlueFerry brings up the link only and leaves DHCP to the user.

The whole feature is off until the user enables it (``SetTethering`` on
``Tether1``, saved in ``settings.json``; ``BLUEFERRY_TETHER_ENABLED`` seeds the
first value). While it is off the controller does not watch or adopt PAN
links, does not hold back adapter recovery, and refuses ``Connect``. Even
when enabled, tethering is never started implicitly unless the user also
turns on automatic tethering. It is layered on the Classic link that the
bearer supervisor already maintains. It only ever talks to ``Network1`` or
NetworkManager, never to the device- or bearer-level connect/disconnect
methods or per-profile connects, so it cannot fight the supervisor over the
ACL link or disturb MAP, PBAP, or ANCS. The test suite enforces this.

Nothing here logs or publishes addresses, device names, IP configuration, or
D-Bus error messages (which can embed a device name). Only D-Bus error names
and stable tokens leave this module.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import dbus
import dbus.exceptions

from blueferry import config
from blueferry.errors import NotReadyError
from blueferry.settings_store import SettingsStore

log = logging.getLogger(__name__)


class TetherDisabledError(NotReadyError):
    """``Connect`` while the user has not enabled Bluetooth tethering."""

BLUEZ = "org.bluez"
NETWORK_IFACE = "org.bluez.Network1"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# ---- public state ---------------------------------------------------------

OFF = "off"
CONNECTING = "connecting"
CONNECTED = "connected"
DISCONNECTING = "disconnecting"
FAILED = "failed"
STATES = frozenset({OFF, CONNECTING, CONNECTED, DISCONNECTING, FAILED})
ACTIVE_STATES = frozenset({CONNECTING, CONNECTED, DISCONNECTING})

BACKEND_NETWORKMANAGER = "networkmanager"
BACKEND_BLUEZ = "bluez"
BACKEND_MODES = frozenset({"auto", BACKEND_NETWORKMANAGER, BACKEND_BLUEZ})

# ---- stable error tokens ---------------------------------------------------
# Clients translate these into user guidance; they never carry device data.

HOTSPOT_REFUSED = "hotspot-refused"
"""The PAN connection failed with BlueZ's generic ``org.bluez.Error.Failed``.
That usually means Personal Hotspot is off, but ``Failed`` is not specific
enough to be sure."""
NOT_SUPPORTED = "not-supported"
"""No usable PAN service: the phone offers no NAP record, or BlueZ lacks its
network plugin (kernel BT_BNEP)."""
PHONE_UNREACHABLE = "phone-unreachable"
IN_PROGRESS = "in-progress"
TIMEOUT = "timeout"
BLUETOOTH_UNAVAILABLE = "bluetooth-unavailable"
PERMISSION_DENIED = "permission-denied"
NO_NETWORK_DEVICE = "no-network-device"
IP_CONFIG_FAILED = "ip-config-failed"
ACTIVATION_FAILED = "activation-failed"
NETWORKMANAGER_UNAVAILABLE = "networkmanager-unavailable"
LINK_LOST = "link-lost"
GENERIC_ERROR = "failed"
ERROR_TOKENS = frozenset({
    HOTSPOT_REFUSED, NOT_SUPPORTED, PHONE_UNREACHABLE, IN_PROGRESS, TIMEOUT,
    BLUETOOTH_UNAVAILABLE, PERMISSION_DENIED, NO_NETWORK_DEVICE,
    IP_CONFIG_FAILED, ACTIVATION_FAILED, NETWORKMANAGER_UNAVAILABLE,
    LINK_LOST, GENERIC_ERROR,
})

# One attempt is bounded even if a backend never answers. NetworkManager
# includes DHCP in its activation, so this is wider than a BlueZ connect.
CONNECT_DEADLINE_SECONDS = 90
AUTOCONNECT_RETRY_SECONDS = 30
AUTOCONNECT_RETRY_CAP_SECONDS = 600
_MAX_INTERFACE_CHARS = 15  # IFNAMSIZ - 1
# How long a connected tether without a known interface may hold back the
# adapter power-cycle recovery.
UNKNOWN_INTERFACE_TRUST_SECONDS = 600
# BNEP teardown can trail the stop request's reply by a moment.
LINK_DOWN_GRACE_SECONDS = 3


def _interface_exists(name: str) -> bool:
    return bool(name) and os.path.exists(os.path.join("/sys/class/net", name))


def dbus_error_name(error: object) -> str:
    if isinstance(error, dbus.exceptions.DBusException):
        return str(error.get_dbus_name() or "")[:256]
    return type(error).__name__[:256]


def _dbus_error_detail(error: object) -> str:
    if isinstance(error, dbus.exceptions.DBusException):
        return str(error.get_dbus_message() or "")[:512].casefold()
    return str(error)[:512].casefold()


_DBUS_TIMEOUT_NAMES = frozenset({
    "org.freedesktop.DBus.Error.NoReply",
    "org.freedesktop.DBus.Error.Timeout",
    "org.freedesktop.DBus.Error.TimedOut",
})
_DBUS_MISSING_SERVICE_NAMES = frozenset({
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.Disconnected",
    "org.freedesktop.DBus.Error.UnknownObject",
})
_DBUS_MISSING_API_NAMES = frozenset({
    "org.freedesktop.DBus.Error.UnknownMethod",
    "org.freedesktop.DBus.Error.UnknownInterface",
})


def bluez_error_token(error: object) -> str:
    """Map a Network1 failure to a public token without exposing its text."""
    name = dbus_error_name(error)
    if name == "org.bluez.Error.Failed":
        detail = _dbus_error_detail(error)
        # Paging the phone failed: out of range or Bluetooth off on the phone.
        if "host is down" in detail or "(112)" in detail or "page timeout" in detail:
            return PHONE_UNREACHABLE
        if "timed out" in detail or "(110)" in detail:
            return TIMEOUT
        # Most likely iOS rejected BNEP because Personal Hotspot is off; the
        # generic Failed name cannot prove it, so clients word this cautiously.
        return HOTSPOT_REFUSED
    if name in {"org.bluez.Error.NotSupported", *_DBUS_MISSING_API_NAMES}:
        return NOT_SUPPORTED
    if name == "org.bluez.Error.InProgress":
        return IN_PROGRESS
    if name in _DBUS_TIMEOUT_NAMES:
        return TIMEOUT
    if name in _DBUS_MISSING_SERVICE_NAMES or name == "org.bluez.Error.NotReady":
        return BLUETOOTH_UNAVAILABLE
    if name == "org.bluez.Error.NotAvailable":
        return NOT_SUPPORTED
    return GENERIC_ERROR


class AsyncBus(Protocol):
    """The two dbus-python connection methods tethering needs.

    Using ``call_async`` directly keeps every step off the GLib loop and gives
    tests one narrow seam for fake BlueZ and NetworkManager services.
    """

    def call_async(
        self, bus_name: str, object_path: str, dbus_interface: str,
        method: str, signature: str | None, args: tuple,
        reply_handler: Callable[..., None],
        error_handler: Callable[[Exception], None],
        timeout: float = -1.0,
    ) -> Any: ...

    def add_signal_receiver(
        self, handler_function: Callable[..., None], signal_name: str | None = None,
        dbus_interface: str | None = None, bus_name: str | None = None,
        path: str | None = None, **keywords: Any,
    ) -> Any: ...


Connected = Callable[[str], None]
Failed = Callable[[str], None]
Lost = Callable[[str, bool], None]
"""An established tether ended: ``(token, user_requested)``."""
Done = Callable[[], None]


class TetherBackend(Protocol):
    """One strategy for bringing the PAN link and its IP configuration up."""

    name: str

    def connect(self, on_connected: Connected, on_error: Failed, on_lost: Lost) -> None: ...

    def disconnect(self, on_done: Done, on_error: Failed) -> None: ...

    def cancel(self) -> None: ...


ChooseBackend = Callable[[Callable[[TetherBackend], None], Failed], None]
Schedule = Callable[[int, Callable[[], bool]], int]
Cancel = Callable[[int], object]


def _safe_interface(value: object) -> str:
    text = str(value or "")
    if len(text) > _MAX_INTERFACE_CHARS or not text.isprintable() or "/" in text:
        return ""
    return text


class NetworkLinkWatch:
    """Observe ``org.bluez.Network1`` on the phone, whoever connected it."""

    def __init__(
        self,
        bus: Callable[[], AsyncBus],
        device_path: str,
        on_change: Callable[[bool, str], None],
    ) -> None:
        self._bus = bus
        self._device_path = device_path
        self._on_change = on_change
        self._match: Any = None
        self._generation = 0

    def start(self) -> None:
        if self._match is None:
            self._match = self._bus().add_signal_receiver(
                self._properties_changed,
                signal_name="PropertiesChanged",
                dbus_interface=PROPERTIES_IFACE,
                bus_name=BLUEZ,
                path=self._device_path,
                arg0=NETWORK_IFACE,
            )
        self.probe()

    def probe(self) -> None:
        """Read the current link once; Network1 may be absent entirely."""
        self._generation += 1
        generation = self._generation

        def reply(properties: Mapping) -> None:
            if generation != self._generation:
                return
            self._on_change(
                bool(properties.get("Connected", False)),
                _safe_interface(properties.get("Interface", "")),
            )

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            log.debug("could not read the phone's PAN link: %s", dbus_error_name(error))
            self._on_change(False, "")

        try:
            self._bus().call_async(
                BLUEZ, self._device_path, PROPERTIES_IFACE, "GetAll", "s",
                (NETWORK_IFACE,), reply, failed, timeout=5.0,
            )
        except Exception as error:
            failed(error)

    def _properties_changed(self, interface, changed, _invalidated) -> None:
        if str(interface) != NETWORK_IFACE or "Connected" not in changed:
            return
        self._generation += 1  # a live signal supersedes an older probe
        self._on_change(
            bool(changed["Connected"]),
            _safe_interface(changed.get("Interface", "")),
        )

    def stop(self) -> None:
        self._generation += 1
        match, self._match = self._match, None
        if match is not None:
            try:
                match.remove()
            except Exception as error:
                log.debug("could not remove PAN link watch: %s", dbus_error_name(error))


class TetherSettings:
    """Persist the tethering opt-in in the owner-only settings document.

    ``BLUEFERRY_TETHER_ENABLED`` and ``BLUEFERRY_TETHER_AUTOCONNECT`` provide
    the initial values. A value saved through the D-Bus API (Qt or GTK
    settings, the TUI, ``blueferry tether enable``) takes precedence.
    """

    ENABLED_KEY = "tether_enabled"
    AUTOCONNECT_KEY = "tether_autoconnect"

    def __init__(
        self,
        path: Path | None = None,
        *,
        default_enabled: bool | None = None,
        default_autoconnect: bool | None = None,
    ) -> None:
        self._settings = SettingsStore(path or config.SETTINGS_JSON)
        payload = self._settings.read()  # never raises; {} when unreadable
        seeds = (
            (self.ENABLED_KEY, "BLUEFERRY_TETHER_ENABLED",
             config.TETHER_ENABLED if default_enabled is None else default_enabled,
             default_enabled is None),
            (self.AUTOCONNECT_KEY, "BLUEFERRY_TETHER_AUTOCONNECT",
             config.TETHER_AUTOCONNECT if default_autoconnect is None
             else default_autoconnect,
             default_autoconnect is None),
        )
        values: list[bool] = []
        for key, name, seeded, from_environment in seeds:
            stored = payload.get(key)
            value = stored if isinstance(stored, bool) else bool(seeded)
            if from_environment and isinstance(stored, bool):
                self._note_overridden(name, bool(seeded), value)
            values.append(value)
        self._enabled, self._autoconnect = values

    @staticmethod
    def _note_overridden(name: str, seeded: bool, effective: bool) -> None:
        """Say once, at startup, that a set seed value is not in effect."""
        if name in os.environ and seeded != effective:
            log.info(
                "%s is ignored because a tethering preference was saved in "
                "settings.json (using %s)",
                name,
                effective,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def autoconnect(self) -> bool:
        return self._autoconnect

    def set(self, enabled: bool, autoconnect: bool) -> tuple[bool, bool]:
        if not isinstance(enabled, bool) or not isinstance(autoconnect, bool):
            raise ValueError("tethering settings must be booleans")
        self._settings.update(**{
            self.ENABLED_KEY: enabled,
            self.AUTOCONNECT_KEY: autoconnect,
        })
        self._enabled = enabled
        self._autoconnect = autoconnect
        return enabled, autoconnect


class TetherController:
    """Explicit, content-free tethering state machine owned by the daemon.

    States: ``off`` → ``connecting`` → ``connected`` → ``disconnecting`` →
    ``off``; a failed attempt ends in ``failed`` with an error token. Every
    backend callback carries the generation that started it, so a late reply
    from a cancelled attempt cannot resurrect it.
    """

    def __init__(
        self,
        choose_backend: ChooseBackend,
        *,
        link_watch: NetworkLinkWatch | None = None,
        classic_ready: Callable[[], bool] = lambda: True,
        autoconnect_ready: Callable[[], bool] = lambda: True,
        on_changed: Callable[[], None] | None = None,
        enabled: bool = False,
        autoconnect: bool = False,
        schedule: Schedule | None = None,
        cancel: Cancel | None = None,
        clock: Callable[[], float] = time.monotonic,
        interface_exists: Callable[[str], bool] = _interface_exists,
    ) -> None:
        if schedule is None or cancel is None:
            from gi.repository import GLib

            schedule = schedule or GLib.timeout_add_seconds
            cancel = cancel or GLib.source_remove
        self._choose_backend = choose_backend
        self._link_watch = link_watch
        self._classic_ready = classic_ready
        self._autoconnect_ready = autoconnect_ready
        self._on_changed = on_changed
        self._enabled = bool(enabled)
        self._autoconnect = autoconnect
        self._schedule = schedule
        self._cancel = cancel
        self._clock = clock
        self._interface_exists = interface_exists
        self._state = OFF
        self._error = ""
        self._interface = ""
        self._backend: TetherBackend | None = None
        self._external = False
        self._generation = 0
        self._deadline_id: int | None = None
        self._retry_id: int | None = None
        self._confirm_id: int | None = None
        self._auto_failures = 0
        self._connected_at: float | None = None
        # Stopping an adopted link cannot tell from NetworkManager's reply
        # whether the link really went down; BlueZ decides (see _disconnected).
        self._awaiting_link_down = False
        self._link_down_grace_spent = False
        # An explicit Disconnect() is a user decision; automatic attempts wait
        # for the next explicit Connect() or a new daemon generation.
        self._auto_suppressed = False
        # Enabling with autoconnect waits for the watch's first report, so a
        # link another tool already holds is adopted, never claimed as ours.
        self._autoconnect_after_probe = False
        self._running = False
        self._watching = False

    # ---- read side -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state in ACTIVE_STATES

    @property
    def enabled(self) -> bool:
        return self._enabled

    def link_alive(self) -> bool:
        """True only while a tether is demonstrably carrying the user's traffic.

        Recovery must not be blocked forever by a stale state: a known
        interface has to exist in the kernel, and an unknown one is trusted
        for a bounded time only.
        """
        if not self._enabled or self._state != CONNECTED:
            return False
        if self._interface:
            return self._interface_exists(self._interface)
        since = self._connected_at
        return since is not None and self._clock() - since < UNKNOWN_INTERFACE_TRUST_SECONDS

    def probe_link(self) -> None:
        """Ask BlueZ for the current link, e.g. before recovery trusts it."""
        if self._state == CONNECTED and self._watching and self._link_watch is not None:
            self._link_watch.probe()

    def snapshot(self) -> dict[str, object]:
        backend = self._backend.name if self._backend is not None else ""
        connected = self._state == CONNECTED
        return {
            "state": self._state,
            "interface": self._interface if connected else "",
            "backend": "" if self._external else backend,
            "external": bool(self._external and connected),
            "error": self._error,
            # Without NetworkManager BlueFerry only brings the link up.
            "needs_dhcp": bool(
                connected and not self._external and backend == BACKEND_BLUEZ
            ),
            "enabled": self._enabled,
            "autoconnect": self._autoconnect,
        }

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._enabled:
            self._start_watch()

    def stop(self) -> None:
        """Forget local state; a NetworkManager tether outlives the daemon."""
        self._running = False
        self._generation += 1
        self._cancel_deadline()
        self._cancel_retry()
        self._cancel_confirm()
        if self._backend is not None:
            self._backend.cancel()
        self._stop_watch()

    def _start_watch(self) -> None:
        if self._watching or not self._running or self._link_watch is None:
            return
        self._watching = True
        self._link_watch.start()

    def _stop_watch(self) -> None:
        if not self._watching or self._link_watch is None:
            self._watching = False
            return
        self._watching = False
        self._link_watch.stop()

    def configure(self, enabled: bool, autoconnect: bool) -> dict[str, object]:
        """Apply the saved opt-in at runtime and return the new snapshot.

        Turning the feature off ends a tether this daemon started, stops
        watching (and so forgets any adopted) PAN link, and releases the
        recovery hold. A link another tool started is left alone.
        """
        enabled = bool(enabled)
        autoconnect = bool(autoconnect)
        was_enabled = self._enabled
        was_auto = self._autoconnect
        self._enabled = enabled
        self._autoconnect = autoconnect
        if not enabled:
            self._disable()
        elif not was_enabled:
            log.info("Bluetooth tethering enabled")
            self._start_watch()
        if enabled and autoconnect and not (was_enabled and was_auto):
            # Turning automatic tethering on is an explicit user decision.
            self._auto_suppressed = False
            self._auto_failures = 0
            if not was_enabled and self._link_watch is not None:
                self._autoconnect_after_probe = True
            else:
                self.maybe_autoconnect()
        elif not autoconnect:
            self._autoconnect_after_probe = False
            self._cancel_retry()
        if (enabled, autoconnect) != (was_enabled, was_auto):
            self._publish()
        return self.snapshot()

    def _disable(self) -> None:
        self._cancel_retry()
        self._autoconnect_after_probe = False
        awaiting_link_down = self._awaiting_link_down
        if self._state in (CONNECTING, CONNECTED) and not self._external:
            log.info("Bluetooth tethering disabled; stopping its link")
            self.disconnect()
        elif self._state != DISCONNECTING or awaiting_link_down:
            # An adopted link belongs to whoever started it: forget it, never
            # stop it. A stop already waiting for BlueZ is settled as done.
            self._generation += 1
            self._cancel_deadline()
            self._external = False
            self._set(OFF, error="", interface="")
            log.info("Bluetooth tethering disabled")
        # Otherwise a stop is in flight; its backend reply finishes it.
        self._awaiting_link_down = False
        self._cancel_confirm()
        self._stop_watch()

    def reset_after_bluez_restart(self) -> None:
        """BlueZ restarts drop every BNEP link and every pending reply."""
        self._generation += 1
        self._cancel_deadline()
        self._cancel_confirm()
        self._awaiting_link_down = False
        if self._backend is not None:
            self._backend.cancel()
        self._backend = None
        was_active = self.active
        was_external = self._external
        self._external = False
        self._set(OFF, error=LINK_LOST if was_active else "", interface="")
        if self._watching and self._link_watch is not None:
            self._link_watch.probe()
        if was_active and not was_external:
            self._schedule_auto_retry()

    # ---- commands ----------------------------------------------------------

    def connect(self, *, automatic: bool = False) -> dict[str, object]:
        if not self._enabled:
            raise TetherDisabledError(
                "Bluetooth tethering is turned off; enable it in BlueFerry's "
                "iPhone settings or with 'blueferry tether enable'"
            )
        if not automatic:
            self._auto_suppressed = False
            self._auto_failures = 0
            self._cancel_retry()
        if self._state in (CONNECTING, CONNECTED):
            return self.snapshot()
        if self._state == DISCONNECTING:
            raise NotReadyError("tethering is still disconnecting; try again shortly")
        if not self._classic_ready():
            raise NotReadyError(
                "the iPhone is not connected over Bluetooth yet; tethering "
                "uses the existing connection"
            )
        self._generation += 1
        generation = self._generation
        self._external = False
        self._set(CONNECTING, error="", interface="")
        self._deadline_id = self._schedule(
            CONNECT_DEADLINE_SECONDS, lambda: self._deadline(generation)
        )
        log.info("starting Bluetooth tethering%s", " (automatic)" if automatic else "")

        def chosen(backend: TetherBackend) -> None:
            if generation != self._generation:
                backend.cancel()
                return
            self._backend = backend
            log.info("tethering through %s", backend.name)
            self._publish()
            try:
                backend.connect(
                    lambda interface: self._connected(generation, interface),
                    lambda token: self._failed(generation, token),
                    lambda token, user_requested: self._lost(
                        generation, token, user_requested
                    ),
                )
            except Exception as error:
                log.warning("tethering backend failed to start: %s", dbus_error_name(error))
                self._failed(generation, GENERIC_ERROR)

        try:
            self._choose_backend(chosen, lambda token: self._failed(generation, token))
        except Exception as error:
            log.warning("could not choose a tethering backend: %s", dbus_error_name(error))
            self._failed(generation, GENERIC_ERROR)
        return self.snapshot()

    def disconnect(self) -> dict[str, object]:
        self._auto_suppressed = True
        self._cancel_retry()
        if self._state == DISCONNECTING:
            return self.snapshot()
        if self._state in (OFF, FAILED):
            # Nothing of ours is up; clear a stale failure for the next attempt.
            self._set(OFF, error="", interface="")
            return self.snapshot()
        self._generation += 1
        generation = self._generation
        self._cancel_deadline()
        self._cancel_confirm()
        backend = self._backend
        external = self._external
        self._set(DISCONNECTING)
        log.info("stopping Bluetooth tethering")

        def run(selected: TetherBackend) -> None:
            if generation != self._generation:
                return
            self._backend = selected
            try:
                selected.disconnect(
                    lambda: self._disconnected(generation, external),
                    lambda token: self._disconnect_failed(generation, token),
                )
            except Exception as error:
                log.warning("tethering disconnect failed to start: %s", dbus_error_name(error))
                self._disconnect_failed(generation, GENERIC_ERROR)

        if backend is not None and not external:
            run(backend)
        else:
            # Adopted after a daemon restart or started by another tool.
            try:
                self._choose_backend(
                    run, lambda token: self._disconnect_failed(generation, token)
                )
            except Exception as error:
                log.warning("could not choose a tethering backend: %s", dbus_error_name(error))
                self._disconnect_failed(generation, GENERIC_ERROR)
        return self.snapshot()

    def maybe_autoconnect(self) -> None:
        """Start one automatic attempt when the user opted in and it is safe."""
        if (
            not self._enabled or not self._autoconnect or self._auto_suppressed
            or not self._running
            or self._state not in (OFF, FAILED) or self._retry_id is not None
        ):
            return
        if not self._classic_ready() or not self._autoconnect_ready():
            return
        try:
            self.connect(automatic=True)
        except NotReadyError:
            return

    # ---- backend callbacks -------------------------------------------------

    def _connected(self, generation: int, interface: str) -> None:
        if generation != self._generation or self._state != CONNECTING:
            return
        self._cancel_deadline()
        self._auto_failures = 0
        self._connected_at = self._clock()
        self._set(CONNECTED, error="", interface=_safe_interface(interface))
        log.info("Bluetooth tethering connected")

    def _failed(self, generation: int, token: str) -> None:
        if generation != self._generation:
            return
        self._generation += 1
        self._cancel_deadline()
        token = token if token in ERROR_TOKENS else GENERIC_ERROR
        log.warning("Bluetooth tethering failed: %s", token)
        backend = self._backend
        if backend is not None:
            backend.cancel()
        self._set(FAILED, error=token, interface="")
        self._schedule_auto_retry()

    def _lost(self, generation: int, token: str, user_requested: bool) -> None:
        """The backend saw an established tether end."""
        if generation != self._generation:
            return
        self._generation += 1
        self._cancel_confirm()
        if user_requested:
            # Someone deliberately turned it off (e.g. in the network applet).
            # Respect that exactly like an explicit Disconnect().
            log.info("Bluetooth tethering was turned off outside BlueFerry")
            self._auto_suppressed = True
            self._cancel_retry()
            self._set(OFF, error="", interface="")
            return
        if self._state != CONNECTED:
            return  # the link watch already reported the loss
        token = token if token in ERROR_TOKENS else LINK_LOST
        log.warning("Bluetooth tethering ended: %s", token)
        self._set(OFF, error=token, interface="")
        self._schedule_auto_retry()

    def _deadline(self, generation: int) -> bool:
        self._deadline_id = None
        if generation == self._generation and self._state == CONNECTING:
            backend = self._backend
            self._failed(generation, TIMEOUT)
            # Withdraw whatever may still complete after the deadline.
            if backend is not None:
                try:
                    backend.disconnect(lambda: None, lambda _token: None)
                except Exception as error:
                    log.debug(
                        "could not withdraw a timed-out tether: %s", dbus_error_name(error)
                    )
        return False

    def _disconnected(self, generation: int, external: bool) -> None:
        if generation != self._generation:
            return
        if external and self._link_watch is not None and self._watching:
            # The backend may have found nothing it recognised to stop. Only
            # BlueZ can say whether the adopted link is really gone.
            self._awaiting_link_down = True
            self._link_down_grace_spent = False
            self._link_watch.probe()
            return
        self._finish_disconnect()

    def _finish_disconnect(self) -> None:
        self._awaiting_link_down = False
        self._cancel_confirm()
        self._external = False
        self._set(OFF, error="", interface="")
        log.info("Bluetooth tethering stopped")

    def _disconnect_failed(self, generation: int, token: str) -> None:
        if generation != self._generation:
            return
        if not self._enabled:
            # Disabled mid-stop: nothing is tracked any more, so do not leave
            # a stale failure behind for the next enable.
            log.warning("could not stop Bluetooth tethering after disabling it")
            self._set(OFF, error="", interface="")
            return
        token = token if token in ERROR_TOKENS else GENERIC_ERROR
        log.warning("could not stop Bluetooth tethering: %s", token)
        self._set(FAILED, error=token, interface="")
        # The link watch corrects the state if the link is actually still up.
        if self._link_watch is not None and self._watching:
            self._link_watch.probe()

    def observe_link(self, connected: bool, interface: str) -> None:
        """Reconcile with BlueZ's Network1 state (ground truth for the link)."""
        if not self._enabled:
            # Off means off: never adopt or track a PAN link.
            return
        self._observe_link(connected, interface)
        if self._autoconnect_after_probe:
            self._autoconnect_after_probe = False
            self.maybe_autoconnect()

    def _observe_link(self, connected: bool, interface: str) -> None:
        if self._awaiting_link_down and self._state == DISCONNECTING:
            self._link_after_disconnect(connected, interface)
            return
        if connected:
            if self._state in (OFF, FAILED):
                # A tether that survived a daemon restart or that another tool
                # started. Report it honestly instead of claiming it is off.
                self._external = True
                self._connected_at = self._clock()
                self._set(CONNECTED, error="", interface=_safe_interface(interface))
            elif self._state == CONNECTED and interface and not self._interface:
                self._set(CONNECTED, interface=_safe_interface(interface))
            return
        if self._state == CONNECTED:
            # Keep the generation: a NetworkManager "user disconnected" report
            # for this same session may still arrive and must win.
            external = self._external
            self._external = False
            log.warning("Bluetooth tethering link was lost")
            self._set(OFF, error=LINK_LOST, interface="")
            if not external:
                # Never chase a link another tool owned and ended.
                self._schedule_auto_retry()

    def _link_after_disconnect(self, connected: bool, interface: str) -> None:
        if not connected:
            self._finish_disconnect()
            return
        if not self._link_down_grace_spent:
            # BNEP teardown trails NetworkManager's reply; look once more.
            self._link_down_grace_spent = True
            self._cancel_confirm()

            def recheck() -> bool:
                self._confirm_id = None
                if self._awaiting_link_down and self._link_watch is not None:
                    self._link_watch.probe()
                return False

            self._confirm_id = self._schedule(LINK_DOWN_GRACE_SECONDS, recheck)
            return
        # Still up: nothing we could identify was stopped. Say so.
        self._awaiting_link_down = False
        self._external = True
        log.warning("Bluetooth tethering is still up after the stop request")
        self._set(CONNECTED, error=GENERIC_ERROR, interface=_safe_interface(interface))

    # ---- helpers -----------------------------------------------------------

    def _set(self, state: str, *, error: str | None = None, interface: str | None = None) -> None:
        changed = state != self._state
        self._state = state
        if state != CONNECTED:
            self._connected_at = None
        if error is not None and error != self._error:
            self._error = error
            changed = True
        if interface is not None and interface != self._interface:
            self._interface = interface
            changed = True
        if changed:
            self._publish()

    def _publish(self) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                log.exception("tethering change notification failed")

    def _cancel_source(self, source_id: int | None, what: str) -> None:
        if source_id is None:
            return
        try:
            self._cancel(source_id)
        except Exception as error:
            log.debug("could not remove tethering %s: %s", what, dbus_error_name(error))

    def _cancel_deadline(self) -> None:
        self._cancel_source(self._deadline_id, "deadline")
        self._deadline_id = None

    def _cancel_retry(self) -> None:
        self._cancel_source(self._retry_id, "retry")
        self._retry_id = None

    def _cancel_confirm(self) -> None:
        self._cancel_source(self._confirm_id, "link check")
        self._confirm_id = None

    def _schedule_auto_retry(self) -> None:
        if (
            not self._enabled or not self._autoconnect or self._auto_suppressed
            or not self._running
        ):
            return
        if self._retry_id is not None:
            return
        delay = min(
            AUTOCONNECT_RETRY_CAP_SECONDS,
            AUTOCONNECT_RETRY_SECONDS * (2 ** min(self._auto_failures, 10)),
        )
        self._auto_failures += 1

        def retry() -> bool:
            self._retry_id = None
            self.maybe_autoconnect()
            return False

        self._retry_id = self._schedule(delay, retry)
