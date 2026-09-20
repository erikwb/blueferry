"""A last-resort adapter reset with durable limits and positive health evidence."""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import dbus
from gi.repository import GLib

from blueferry.bus import get_system_bus, obex
from blueferry.obex.worker import ObexWorker
from blueferry.settings_store import BLUETOOTH_RECOVERY_KEY, SettingsStore

log = logging.getLogger(__name__)

POLL_SECONDS = 10
OUTAGE_SECONDS = 300
SOFT_RESET_SECONDS = 180
RESET_SETTLE_SECONDS = 60
REARM_SECONDS = 600
HOURLY_LIMIT_SECONDS = 3600
HEALTH_PROBE_SECONDS = 60
HEALTH_FRESH_SECONDS = 90
SETTINGS_KEY = BLUETOOTH_RECOVERY_KEY


@dataclass(frozen=True)
class AdapterState:
    owner: str
    address: str
    powered: bool
    safe: bool
    power_state: str = "on"
    revision: int = 0
    incarnation: int = 0


class BluezRecoveryAdapter:
    """Use only the selected controller, pinned to its BlueZ owner and address."""

    def __init__(self, adapter: str, phone: str) -> None:
        self.path = f"/org/bluez/{adapter}"
        self.device_path = f"{self.path}/dev_{phone.replace(':', '_')}"
        self._revision = 0
        self._incarnation = 0
        self._matches: list = []
        self._restore: AdapterState | None = None
        self._power_on_replied = False

    @property
    def restore_pending(self) -> bool:
        return self._restore is not None

    def start_monitoring(self) -> None:
        """Observe topology on GLib's connection before collecting evidence."""
        if self._matches:
            return
        bus = get_system_bus()
        try:
            for signal in ("InterfacesAdded", "InterfacesRemoved"):
                self._matches.append(bus.add_signal_receiver(
                    self._interfaces_changed,
                    dbus_interface="org.freedesktop.DBus.ObjectManager",
                    signal_name=signal, bus_name="org.bluez", path="/",
                ))
            self._matches.append(bus.add_signal_receiver(
                self._properties_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged", bus_name="org.bluez",
                path_keyword="path",
            ))
            self._matches.append(bus.add_signal_receiver(
                self._owner_changed,
                dbus_interface="org.freedesktop.DBus",
                signal_name="NameOwnerChanged", bus_name="org.freedesktop.DBus",
                arg0="org.bluez",
            ))
        except Exception:
            self.stop_monitoring()
            raise

    def stop_monitoring(self) -> None:
        for match in self._matches:
            try:
                match.remove()
            except Exception:
                log.debug("could not remove Bluetooth recovery monitor", exc_info=True)
        self._matches.clear()

    def _interfaces_changed(self, path, interfaces) -> None:
        if path == self.path and "org.bluez.Adapter1" in interfaces:
            self._incarnation += 1
            self._revision += 1
        elif str(path).startswith(self.path + "/") and any(
            name in interfaces for name in (
                "org.bluez.Device1", "org.bluez.Bearer.LE1", "org.bluez.Bearer.BREDR1",
            )
        ):
            self._revision += 1

    def _owner_changed(self, _name, _old, _new) -> None:
        self._incarnation += 1
        self._revision += 1

    def _properties_changed(self, interface, changed, invalidated, *, path) -> None:
        names = set(changed) | set(invalidated)
        relevant = False
        if path == self.path and interface == "org.bluez.Adapter1":
            relevant = bool(names & {"Address", "Powered", "PowerState",
                                     "Discovering", "Discoverable"})
        elif str(path).startswith(self.path + "/"):
            if interface == "org.bluez.Device1":
                watched = {"Paired", "Bonded", "Blocked"}
                if path != self.device_path:
                    watched.add("Connected")
                relevant = bool(names & watched)
            elif path != self.device_path and interface in {
                "org.bluez.Bearer.LE1", "org.bluez.Bearer.BREDR1",
            }:
                relevant = "Connected" in names
        if relevant:
            self._revision += 1

    def read(self) -> AdapterState:
        revision = self._revision
        incarnation = self._incarnation
        bus = get_system_bus()
        owner = str(bus.get_name_owner("org.bluez"))
        manager = dbus.Interface(bus.get_object(owner, "/", introspect=False),
                                 "org.freedesktop.DBus.ObjectManager")
        objects = manager.GetManagedObjects(timeout=5.0)
        props = objects[self.path]["org.bluez.Adapter1"]
        phone = objects.get(self.device_path, {}).get("org.bluez.Device1", {})
        powered = bool(props["Powered"])
        safe = (
            powered
            # Without PowerState a timed-out Set(False) cannot be distinguished
            # from an idle, powered controller. Do not start that transaction.
            and str(props.get("PowerState", "")) == "on"
            and not props.get("Discovering", True)
            and not props.get("Discoverable", True)
            and bool(phone.get("Bonded", phone.get("Paired", False)))
            and not phone.get("Blocked", False)
        )
        for path, interfaces in objects.items():
            if not str(path).startswith(self.path + "/") or str(path) == self.device_path:
                continue
            device = interfaces.get("org.bluez.Device1")
            if device is not None and (
                device.get("Connected", True)
                or device.get("Paired", False)
                or device.get("Bonded", False)
                or interfaces.get("org.bluez.Bearer.LE1", {}).get("Connected", False)
                or interfaces.get("org.bluez.Bearer.BREDR1", {}).get("Connected", False)
            ):
                safe = False
        if revision != self._revision:
            raise RuntimeError("adapter topology changed during inspection")
        return AdapterState(owner, str(props["Address"]).upper(), powered, safe,
                            str(props.get("PowerState", "")), revision, incarnation)

    def _properties(self, owner: str):
        # Resolve the proxy before the final snapshot, and avoid asynchronous
        # introspection inserting another round trip between that check and Set.
        return dbus.Interface(
            get_system_bus().get_object(owner, self.path, introspect=False),
            "org.freedesktop.DBus.Properties",
        )

    def cycle(self, expected: AdapterState, cancelled: threading.Event) -> None:
        """Run on the reserved worker; shutdown waits for the restore attempt.

        D-Bus has no atomic identity-check-and-Set operation. Resolve the proxy
        first, recheck the snapshot and signal revision immediately before Set,
        and decline shared adapters even when their other bonds are disconnected.
        """
        if self.restore_pending:
            raise RuntimeError("adapter power restoration is still pending")
        props = self._properties(expected.owner)
        current = self.read()
        if (cancelled.is_set() or current != expected or not current.safe
                or self._revision != current.revision):
            raise RuntimeError("adapter recovery conditions changed")
        # Set can reach BlueZ even when its reply times out. Keep restoration
        # independent of the spent cycle budget until power is known to settle.
        self._restore = expected
        self._power_on_replied = False
        try:
            props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(False), timeout=15.0)
        finally:
            self.restore()

    def restore(self) -> None:
        """One bounded attempt; transient failures retain the restoration intent.

        Only the worker accesses this transaction. GLib checks restore_pending
        after its completion and retries without ever sending another power-off.
        """
        expected = self._restore
        if expected is None:
            return
        if self._incarnation != expected.incarnation:
            self._restore = None
            raise RuntimeError("controller changed during Bluetooth recovery")
        props = self._properties(expected.owner)
        current = self._restoration_state(expected)
        if current.power_state not in {"on", "off"}:
            return  # Includes pending transitions and missing/unknown state.
        if not current.powered and current.power_state == "off":
            if self._power_on_replied:
                self._restore = None
                raise RuntimeError("Bluetooth was turned off after restoration")
            if self._incarnation != expected.incarnation:
                self._restore = None
                raise RuntimeError("controller changed during Bluetooth recovery")
            props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True), timeout=15.0)
            self._power_on_replied = True
            # A successful reply alone does not prove the final state. Read
            # once more; an error or pending transition keeps restoration owed.
            current = self._restoration_state(expected)
        if current.powered and current.power_state == "on":
            self._restore = None

    def _restoration_state(self, expected: AdapterState) -> AdapterState:
        current = self.read()
        if (current.owner != expected.owner or current.address != expected.address
                or current.incarnation != expected.incarnation):
            self._restore = None
            raise RuntimeError("controller changed during Bluetooth recovery")
        if current.power_state == "off-blocked":
            self._restore = None
            raise RuntimeError("adapter power could not be restored: rfkill blocked")
        return current

    def finish_shutdown(self) -> None:
        """Give an interrupted restoration a bounded final chance on the worker."""
        deadline = time.monotonic() + 30
        while self.restore_pending and time.monotonic() < deadline:
            try:
                self.restore()
            except Exception:
                log.debug("Bluetooth restoration during shutdown failed", exc_info=True)
            if self.restore_pending:
                time.sleep(0.2)
        if self.restore_pending:
            log.error("Bluetooth power restoration is unfinished at daemon shutdown")


def probe_map(session_path: str) -> None:
    """Issue an actual OBEX GET, without reading messages or changing folders."""
    obex(session_path, "org.bluez.obex.MessageAccess1").ListFolders(
        {"MaxCount": dbus.UInt16(1)}, timeout=15.0,
    )


@dataclass(frozen=True)
class RecoveryObservation:
    healthy: bool
    health_proof: float | None
    eligible: bool
    busy: bool = False


class BluetoothRecovery:
    """Permit one power cycle per outage; only sustained ANCS health rearms it."""

    def __init__(
        self,
        phone: str,
        adapter: BluezRecoveryAdapter,
        worker: ObexWorker,
        *,
        observe: Callable[[], RecoveryObservation],
        probe: Callable[[], None],
        probe_health: Callable[[], None],
        reset_le: Callable[[], None],
        pause: Callable[[], None],
        resume: Callable[[], None],
        settings: SettingsStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        schedule=GLib.timeout_add_seconds,
        idle=GLib.idle_add,
        cancel=GLib.source_remove,
    ) -> None:
        self.phone = phone.upper()
        self.adapter = adapter
        self.worker = worker
        self._observe = observe
        self._probe = probe
        self._probe_health = probe_health
        self._reset_le = reset_le
        self._pause = pause
        self._resume = resume
        self._settings = settings or SettingsStore()
        self._clock = clock
        self._wall_clock = wall_clock
        self._schedule = schedule
        self._idle = idle
        self._cancel = cancel
        raw = self._settings.read().get(SETTINGS_KEY, {})
        self._record = raw if isinstance(raw, dict) else {}
        self._running = False
        self._suspended = False
        self._timer: int | None = None
        self._generation = 0
        self._identity: tuple[str, str] | None = None
        self._proof_floor = self._clock()
        self._outage_since: float | None = None
        self._healthy_since: float | None = None
        self._last_observation = self._clock()
        self._soft_reset_at: float | None = None
        self._probing = False
        self.active = False
        self._operation_pending = False
        self._restore_wait_logged = False
        self._resume_pending = False
        self._cancelled = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self.adapter.start_monitoring()
        self._running = True
        self._timer = self._schedule(POLL_SECONDS, self._tick)

    def invalidate(self, *, suspended: bool | None = None) -> None:
        """Discard observations across sleep, owner changes, and user power-off."""
        self._generation += 1
        if suspended is not None:
            self._suspended = suspended
        self._outage_since = None
        self._healthy_since = None
        self._soft_reset_at = None
        self._probing = False
        self._proof_floor = self._clock()
        if self.active:
            self._cancelled.set()
        elif self._running and suspended is False and self._resume_pending:
            self._resume_pending = False
            self._resume()

    def stop(self) -> None:
        self._running = False
        self.invalidate()
        self.adapter.stop_monitoring()
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None

    def forget_phone(self) -> None:
        """A removed bond needs fresh ANCS evidence; retain the hourly budget."""
        self.invalidate()
        self._save(verified=False, spent=True)

    def _save(self, **updates) -> bool:
        record = {**self._record, **updates}
        try:
            self._settings.update(**{SETTINGS_KEY: record})
        except (OSError, ValueError):
            log.warning("cannot persist Bluetooth recovery limit; leaving radio alone",
                        exc_info=True)
            return False
        self._record = record
        return True

    def _known(self, state: AdapterState) -> bool:
        return (
            self._record.get("adapter") == state.address
            and self._record.get("phone") == self.phone
            and self._record.get("verified") is True
        )

    def _allowed(self, state: AdapterState) -> bool:
        last = self._record.get("last_attempt", 0)
        return (
            self._known(state)
            and self._record.get("spent") is False
            and type(last) in (int, float)
            and math.isfinite(last)
            and self._wall_clock() - last >= HOURLY_LIMIT_SECONDS
        )

    def _tick(self) -> bool:
        if not self._running:
            return False
        if self._suspended:
            return True
        if self.active:
            if not self._operation_pending:
                self._operation_pending = True
                try:
                    self.worker.submit(
                        self.adapter.restore, reserved=True,
                        on_success=lambda _result: self._finished(None),
                        on_error=self._finished,
                    )
                except Exception as error:
                    self._finished(error)
            return True
        try:
            self._check()
        except Exception:
            # Missing properties, bus errors, and unplugged controllers provide
            # no evidence of a stuck phone. Start a fresh observation window.
            log.debug("Bluetooth recovery observation unavailable", exc_info=True)
            self.invalidate()
        return True

    def _check(self) -> None:
        state = self.adapter.read()
        identity = (state.owner, state.address)
        if identity != self._identity:
            self.invalidate()
            self._identity = identity
        if not state.powered:
            self.invalidate()
            return
        now = self._clock()
        if now - self._last_observation > HEALTH_FRESH_SECONDS:
            self._healthy_since = None
            self._outage_since = None
            self._soft_reset_at = None
        self._last_observation = now
        observed = self._observe()
        if observed.healthy:
            self._outage_since = None
            self._soft_reset_at = None
            if self._known(state) and self._record.get("spent") is False:
                return
            proof = observed.health_proof
            if (
                proof is None or proof < self._proof_floor
                or now - proof >= HEALTH_PROBE_SECONDS
            ):
                self._probe_health()
            if proof is None or proof < self._proof_floor or now - proof > HEALTH_FRESH_SECONDS:
                self._healthy_since = None
                return
            if self._healthy_since is None:
                self._healthy_since = now
            if not self._known(state):
                self._save(adapter=state.address, phone=self.phone, verified=True, spent=False)
            elif (
                self._record.get("spent") is not False
                and now - self._healthy_since >= REARM_SECONDS
            ):
                self._save(spent=False)
            return
        self._healthy_since = None
        if observed.busy:
            return
        if not state.safe or not observed.eligible or not self._allowed(state):
            self._outage_since = None
            self._soft_reset_at = None
            return
        if self._outage_since is None:
            self._outage_since = now
        if self._probing:
            return
        if self._soft_reset_at is None and now - self._outage_since >= SOFT_RESET_SECONDS:
            self._soft_reset_at = now
            self._reset_le()
            return
        if (
            now - self._outage_since < OUTAGE_SECONDS
            or self._soft_reset_at is None
            or now - self._soft_reset_at < RESET_SETTLE_SECONDS
        ):
            return
        self._probing = True
        generation = self._generation
        try:
            def probe() -> float:
                self._probe()
                return self._clock()

            self.worker.submit(
                probe,
                on_success=lambda when: self._idle(self._after_probe, generation, state, when),
                on_error=lambda _error: self._probe_failed(generation),
            )
        except RuntimeError:
            self._probing = False

    def _probe_failed(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._probing = False
        self._outage_since = None

    def _after_probe(self, generation: int, expected: AdapterState, when: float) -> bool:
        if generation != self._generation or not self._running or self._suspended:
            return False
        self._probing = False
        if (
            self._clock() - when > 30
            or self._outage_since is None
            or self._clock() - self._outage_since < OUTAGE_SECONDS
        ):
            return False
        try:
            current = self.adapter.read()
            observed = self._observe()
            if (
                current != expected or not current.safe or observed.healthy
                or not observed.eligible or observed.busy or not self._allowed(current)
                or not self.worker.reserve_if_idle()
            ):
                return False
        except Exception:
            self.invalidate()
            return False
        # Reserve the budget before any mutation or callback. A failed power
        # cycle is still an attempt and stays spent across process restarts.
        if not self._save(spent=True, last_attempt=self._wall_clock()):
            self.worker.release()
            return False
        self.active = True
        self._operation_pending = True
        self._restore_wait_logged = False
        self._cancelled = threading.Event()
        try:
            self._pause()
            log.warning("ANCS stayed unavailable with MAP responding; cycling Bluetooth once")
            self.worker.submit(
                lambda: self.adapter.cycle(current, self._cancelled),
                reserved=True,
                on_success=lambda _result: self._finished(None),
                on_error=self._finished,
            )
        except Exception as error:
            self._finished(error)
        return False

    def _finished(self, error: Exception | None) -> None:
        self._operation_pending = False
        if self.adapter.restore_pending:
            # Keep profile traffic paused and the worker reserved. This is a
            # continuation of the original transaction, not a second cycle.
            if not self._restore_wait_logged:
                log.warning("Bluetooth power restoration pending; retrying power-on only: %s",
                            error or "power transition still in progress")
                self._restore_wait_logged = True
            elif error is not None:
                log.debug("Bluetooth power restoration still pending: %s", error)
            return
        self.active = False
        self.worker.release()
        self.invalidate(suspended=self._suspended)
        if error is not None:
            log.warning("automatic Bluetooth recovery failed; further cycles disabled: %s", error)
        else:
            log.info("Bluetooth power restored; waiting for verified ANCS recovery")
        if self._running and self._suspended:
            self._resume_pending = True
        elif self._running:
            self._resume()
